from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from typing import List, Optional, Dict, Any
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin, urlparse
import logging
from pydantic import Field

# --- Config ---
REQUEST_TIMEOUT = 8
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ShopifyInsightsFetcher/1.0; +https://example.com/bot)"
}

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shopify-fetcher")

# --- Helper regex ---
EMAIL_RE = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
PHONE_RE = re.compile(r"((?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{5,12})")
SOCIAL_HOSTS = ["instagram.com", "facebook.com", "tiktok.com", "twitter.com", "youtube.com", "pinterest.com", "linkedin.com"]

# --- Pydantic models ---


class Product(BaseModel):
    id: Optional[int] = None
    title: Optional[str] = None
    handle: Optional[str] = None
    url: Optional[str] = None
    variants: List[Dict[str, Any]] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    image: Optional[str] = None
    body_html: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None


class BrandContext(BaseModel):
    website: HttpUrl
    title: Optional[str]
    description: Optional[str]
    products: List[Product] = []
    hero_products: List[Product] = []
    privacy_policy: Optional[str] = None
    return_refund_policy: Optional[str] = None
    faqs: Optional[List[Dict[str,str]]] = []
    socials: Dict[str, str] = {}
    contacts: Dict[str, List[str]] = {}
    important_links: Dict[str, str] = {}
    raw_pages: Dict[str, str] = {}

# --- Utility functions ---
def safe_get(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp
    except requests.HTTPError as e:
        logger.warning(f"HTTP error for {url}: {e}")
        raise
    except Exception as e:
        logger.warning(f"Request failed for {url}: {e}")
        raise

def normalize_base(url: str) -> str:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base

def find_social_links(soup: BeautifulSoup, base: str):
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        for host in SOCIAL_HOSTS:
            if host in href:
                # key = host shortened
                key = host.split(".")[0]
                # avoid duplicates
                if key not in found:
                    found[key] = href if href.startswith("http") else urljoin(base, href)
    return found

def find_emails_phones(text: str):
    emails = list(set(EMAIL_RE.findall(text)))
    phones = list(set([p for p in PHONE_RE.findall(text) if len(re.sub(r"\D","",p))>=6]))
    return emails, phones

def extract_text_from_url(url: str) -> str:
    try:
        r = safe_get(url)
        soup = BeautifulSoup(r.text, "lxml")
        # get visible text
        texts = soup.stripped_strings
        return " ".join(list(texts)[:5000])  # limit length to keep memory sane
    except Exception:
        return ""

def try_products_json(base_url: str):
    
    products = []
    tried_urls = [
        urljoin(base_url, "/products.json?limit=250"),
        urljoin(base_url, "/products.json"),
    ]
    for url in tried_urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "products" in data:
                    products = data["products"]
                    logger.info(f"Found {len(products)} products at {url}")
                    break
        except Exception as e:
            logger.debug(f"products.json failed at {url}: {e}")
    # Basic normalization
    normalized = []
    for p in products:
        price_min = price_max = None
        try:
            variants = p.get("variants", [])
            prices = [float(v.get("price", 0) or 0) for v in variants if v.get("price") is not None]
            if prices:
                price_min, price_max = min(prices), max(prices)
        except:
            pass
        img = None
        imgs = p.get("images") or []
        if imgs:
            img = imgs[0] if isinstance(imgs[0], str) else (imgs[0].get("src") if isinstance(imgs[0], dict) else None)
        prod = {
            "id": p.get("id"),
            "title": p.get("title"),
            "handle": p.get("handle"),
            "url": urljoin(base_url, f"/products/{p.get('handle')}") if p.get("handle") else None,
            "variants": p.get("variants", []),
            "tags": p.get("tags", "").split(",") if isinstance(p.get("tags"), str) else p.get("tags", []),
            "image": img,
            "body_html": p.get("body_html"),
            "price_min": price_min,
            "price_max": price_max
        }
        normalized.append(prod)
    return normalized

def extract_nav_links(soup: BeautifulSoup, base: str):
    links = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = (a.get_text() or "").strip()
        if not href:
            continue
        if href.startswith("#"):
            continue
        full = href if href.startswith("http") else urljoin(base, href)
        links[text or full] = full
    return links

def find_policy_pages(links: Dict[str,str]):
    privacy = None
    returns = None
    for key,url in links.items():
        k = key.lower()
        if "privacy" in k or "privacy" in url.lower():
            privacy = url
        if "return" in k or "refund" in k or "refund" in url.lower() or "returns" in url.lower():
            returns = url
    return privacy, returns

def extract_faqs_from_page(soup: BeautifulSoup):
   
    faqs = []
   
    candidates = soup.find_all(attrs={"class": re.compile("faq", re.I)}) + soup.find_all(attrs={"id": re.compile("faq", re.I)})
    for cand in candidates:
      
        questions = cand.find_all(['h2','h3','h4','dt','summary'])
        for q in questions:
            q_text = q.get_text(strip=True)
            
            nxt = q.find_next_sibling()
            a_text = ""
            if nxt:
                a_text = nxt.get_text(" ", strip=True)
            else:
               
                parent = q.parent
                if parent:
                    a_text = parent.get_text(" ", strip=True).replace(q_text, "").strip()
            if q_text:
                faqs.append({"q": q_text, "a": a_text})
    
    if not faqs:
       
        for det in soup.find_all("details"):
            summary = det.find("summary")
            q_text = summary.get_text(strip=True) if summary else ""
            a_text = det.get_text(" ", strip=True).replace(q_text, "").strip()
            if q_text:
                faqs.append({"q": q_text, "a": a_text})
    return faqs

# --- Core extraction logic ---
def extract_brand_context(website_url: str) -> BrandContext:
    website_url = str(website_url) 
    base = normalize_base(website_url)
    logger.info(f"Extracting for {website_url} (base {base})")

    #fetch homepage
    try:
        resp = safe_get(website_url)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Website not reachable: {e}")

    soup = BeautifulSoup(resp.text, "lxml")
    title = soup.title.string.strip() if soup.title else None
    desc_tag = soup.find("meta", attrs={"name":"description"}) or soup.find("meta", attrs={"property":"og:description"})
    description = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else None

    # 2) try products.json
    products_raw = try_products_json(base)
    products = [Product(**p) for p in products_raw]

    # 3) hero products
    home_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/products/" in href:
            # extract handle if possible
            full = href if href.startswith("http") else urljoin(base, href)
            home_links.add(full)
    hero_products = []
    if home_links:
        for hl in home_links:
            found = None
            for p in products:
                p_url = str(p.url) if p.url else None
                if p_url and (p_url.rstrip("/") in hl.rstrip("/") or (p.handle and p.handle in hl)):
                    found = p
                    break
            if not found:
                handle = hl.split("/products/")[-1].split("?")[0].strip("/")
                found = Product(title=None, handle=handle, url=hl)
            hero_products.append(found)


    # 4) nav links 
    nav_links = extract_nav_links(soup, base)
    privacy_url, returns_url = find_policy_pages(nav_links)

    # If privacy_url not found, try common paths
    if not privacy_url:
        candidate = urljoin(base, "/policies/privacy-policy")  # common
        try:
            r = requests.get(candidate, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                privacy_url = candidate
        except:
            pass
    if not returns_url:
        candidate = urljoin(base, "/policies/refund-policy")
        try:
            r = requests.get(candidate, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                returns_url = candidate
        except:
            pass

    # 5) extract text from privacy and returns pages
    privacy_text = extract_text_from_url(privacy_url) if privacy_url else None
    returns_text = extract_text_from_url(returns_url) if returns_url else None

    # 6) socials
    socials = find_social_links(soup, base)

    # 7) contacts - emails and phones present on homepage + find contact page
    page_text = resp.text
    emails, phones = find_emails_phones(page_text)
    contacts = {"emails": emails, "phones": phones}

    # 8) find contact page and about page text
    contact_url = None
    about_url = None
    tracking_url = None
    blogs = None
    for k,u in nav_links.items():
        key = (k or "").lower()
        if "contact" in key or "contact" in u.lower():
            contact_url = u
        if "about" in key or "about" in u.lower():
            about_url = u
        if "track" in key or "order" in key or "tracking" in key:
            tracking_url = u
        if "blog" in key or "/blogs" in u:
            blogs = u

    about_text = extract_text_from_url(about_url) if about_url else None
    contact_text = extract_text_from_url(contact_url) if contact_url else None

    # add any emails/phones found on contact/about pages
    for text in [about_text, contact_text]:
        if text:
            more_emails, more_phones = find_emails_phones(text)
            for e in more_emails:
                if e not in contacts["emails"]:
                    contacts["emails"].append(e)
            for p in more_phones:
                if p not in contacts["phones"]:
                    contacts["phones"].append(p)

    # 9) FAQs - try nav first then page scanning
    faqs = []
    faq_url = None
    for k,u in nav_links.items():
        if "faq" in (k or "").lower() or "/faq" in u.lower():
            faq_url = u
            break
    if faq_url:
        try:
            r = safe_get(faq_url)
            faqs = extract_faqs_from_page(BeautifulSoup(r.text, "lxml"))
        except Exception:
            faqs = []
    # fallback scan homepage
    if not faqs:
        faqs = extract_faqs_from_page(soup)

    # 10) important links
    important = {}
    if contact_url:
        important["contact"] = contact_url
    if privacy_url:
        important["privacy_policy"] = privacy_url
    if returns_url:
        important["return_refund_policy"] = returns_url
    if tracking_url:
        important["order_tracking"] = tracking_url
    if blogs:
        important["blogs"] = blogs

    # 11) raw_pages (store small text snippets for debugging)
    raw_pages = {}
    if about_text:
        raw_pages["about"] = about_text[:5000]
    if contact_text:
        raw_pages["contact"] = contact_text[:5000]
    if privacy_text:
        raw_pages["privacy_policy"] = privacy_text[:5000]
    if returns_text:
        raw_pages["return_refund_policy"] = returns_text[:5000]

    brand = BrandContext(
        website=website_url,
        title=title,
        description=description,
        products=products,
        hero_products=hero_products,
        privacy_policy=privacy_text,
        return_refund_policy=returns_text,
        faqs=faqs,
        socials=socials,
        contacts=contacts,
        important_links=important,
        raw_pages=raw_pages
    )
    return brand

# --- FastAPI app ---
app = FastAPI(title="Shopify Insights Fetcher - GenAI Dev Intern Demo")

class ExtractRequest(BaseModel):
    website_url: HttpUrl

@app.post("/extract", response_model=BrandContext)
def extract(req: ExtractRequest):
    """
    INPUT: { "website_url": "https://examplestore.com" }
    OUTPUT: BrandContext JSON
    Errors: 401 if website not reachable / not found; 500 for other internal errors
    """
    try:
        brand = extract_brand_context(str(req.website_url)) 
        return brand
    except HTTPException as he:
        
        raise he
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")

@app.get("/")
def root():
    return {
        "message": "Shopify Insights Fetcher. POST /extract { website_url }",
        
    }
