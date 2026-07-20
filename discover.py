"""discover.py
Utilities to discover company career pages focused on a country (e.g. CH).

Strategy:
- use Google Custom Search JSON API (if `GOOGLE_API_KEY` and `GOOGLE_CX` set)
- fallback: check common career page paths on candidate domains

The module returns discovered career page URLs without modifying project data files.
"""
from urllib.parse import urlparse
import os
import requests
from bs4 import BeautifulSoup
import time
import math
import scraper
import json
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")

# persistent cache for discovery results (avoid reprocessing same domains)
CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "discovery_cache.json")
# how long to consider a cached discovery valid
CACHE_TTL_DAYS = 30


def _load_discovery_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_discovery_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _is_cached_recent(cache, domain, ttl_days=CACHE_TTL_DAYS):
    if domain not in cache:
        return False
    try:
        ts = datetime.fromisoformat(cache[domain].get("checked_at"))
        return datetime.utcnow() - ts < timedelta(days=ttl_days)
    except Exception:
        return False


def _update_cache_entry(cache, domain, info=None):
    cache[domain] = {"checked_at": datetime.utcnow().isoformat(), "info": info or {}}
    _save_discovery_cache(cache)


def _extract_domain(url):
    try:
        p = urlparse(url)
        return p.netloc.lower()
    except Exception:
        return None


def google_cse_search(q, num=10):
    """Query Google Custom Search and return list of result URLs.
    Requires env vars `GOOGLE_API_KEY` and `GOOGLE_CX`.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return []
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": q, "num": min(num, 10)}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        return [it.get("link") for it in items if it.get("link")]
    except Exception:
        return []


COMMON_CAREER_PATHS = [
    "/careers",
    "/jobs",
    "/careers/jobs",
    "/company/careers",
    "/en/careers",
    "/en/jobs",
]


def _url_exists(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=6)
        return r.status_code < 400
    except Exception:
        return False


def find_career_page_for_domain(domain):
    """Try common career paths on a domain and return the first existing URL."""
    if not domain:
        return None
    scheme = "https://"
    # try root -- some sites have a /careers page linked from homepage
    root = f"{scheme}{domain}"
    if _url_exists(root):
        # also try to see whether root mentions careers by quick GET
        try:
            r = requests.get(root, timeout=6)
            txt = (r.text or "").lower()
            if "career" in txt or "job" in txt or "vacanc" in txt:
                return root
        except Exception:
            pass

    for p in COMMON_CAREER_PATHS:
        candidate = f"{scheme}{domain}{p}"
        if _url_exists(candidate):
            return candidate
    return None


def discover_pipeline(country="CH", query=None, max_results=20):
    """Discover candidate career pages in the given country.

    - If Google CSE is configured, uses it to find likely career pages scoped
      to the country's TLD (e.g. site:.ch).
    - Returns a list of dicts: {domain, career_url, source}
    """
    results = []
    q = f"site:.{country.lower()} (careers OR jobs OR \"join us\")"
    if query:
        q = q + " " + query

    urls = google_cse_search(q, num=max_results)
    seen = set()
    for u in urls:
        domain = _extract_domain(u)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        career = find_career_page_for_domain(domain)
        results.append({"domain": domain, "career_url": career or u, "source": "google_cse"})
        if len(results) >= max_results:
            break

    return results


def discover_and_scan(source="swissbiotech", q=None, max_results=20, per_site_timeout=120):
    """Run discovery (SwissBiotech or Google CSE) and scan each discovered career page.

    Returns a list of dicts with company info and the postings found:
      {name, domain, profile_url, career_url, postings: [...], error}
    The function does NOT modify `data/sites.json`; results are staging only.
    """
    if source == "swissbiotech":
        candidates = discover_from_swissbiotech(max_companies=max_results)
    else:
        candidates = discover_pipeline(country="CH", query=q, max_results=max_results)

    results = []
    for c in candidates:
        career = c.get("career_url") or c.get("url")
        if not career:
            results.append({**c, "postings": [], "error": "no career_url"})
            continue
        ats = detect_ats(career)
        site_obj = {"id": c.get("domain") or c.get("name"), "name": c.get("name") or c.get("domain"), "url": career}
        try:
            postings, error = scraper.scan_site_with_timeout(site_obj, timeout_s=per_site_timeout)
        except Exception as e:
            postings, error = [], str(e)
        results.append({**c, "postings": postings, "error": error, "ats": ats})
    return results


def discover_from_swissbiotech(index_url="https://www.swissbiotech.org/companies/?type=company&sort=a-z-featured", max_companies=100):
    """Discover companies listed on Swiss Biotech directory.

    For each company profile found on the directory page, attempt to extract
    the official website and probe common career pages.
    Returns a list of dicts: {name, domain, profile_url, career_url, source}
    """
    results = []
    # try a simple requests fetch first; if the site blocks (403) or
    # requests fails, fall back to a Playwright-rendered fetch
    try:
        r = requests.get(index_url, timeout=15)
        r.raise_for_status()
        index_html = r.text
    except Exception:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(user_agent="Mozilla/5.0 (compatible; JobMonitorBot/1.0)")
                page.goto(index_url, timeout=30000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                index_html = page.content()
                browser.close()
        except Exception:
            return results

    soup = BeautifulSoup(index_html or "", "html.parser")
    # find links to company profiles: heuristics including several known patterns
    anchors = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # include patterns observed across directory pages
        if (
            "/companies/" in href
            or "/company/" in href
            or "/companies?" in href
            or "/listing/" in href
            or "/company-listing/" in href
            or "/company-profile/" in href
        ):
            anchors.append(href)

    # normalize profile URLs
    profile_urls = []
    for href in anchors:
        if href.startswith("http"):
            profile_urls.append(href)
        else:
            base = index_url.split("/companies")[0]
            profile_urls.append(base.rstrip("/") + href)

    seen_domains = set()
    # load persistent cache and existing sites to avoid reprocessing
    cache = _load_discovery_cache()
    try:
        existing_sites = scraper.load_sites()
        existing_domains = {_extract_domain(s.get("url", "")) for s in existing_sites}
    except Exception:
        existing_domains = set()
    count = 0
    for pu in profile_urls:
        if count >= max_companies:
            break
        # fetch profile page: prefer requests but fallback to Playwright
        pr_text = None
        try:
            pr = requests.get(pu, timeout=12)
            pr.raise_for_status()
            pr_text = pr.text
        except Exception:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    page = browser.new_page(user_agent="Mozilla/5.0 (compatible; JobMonitorBot/1.0)")
                    page.goto(pu, timeout=30000, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    page.wait_for_timeout(800)
                    pr_text = page.content()
                    browser.close()
            except Exception:
                pr_text = None

        if not pr_text:
            continue
        psoup = BeautifulSoup(pr_text, "html.parser")
        # try to locate an external website link
        website = None
        # common pattern: link with rel external or contains 'website' text
        for a in psoup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("http") and "swissbiotech.org" not in href:
                text = (a.get_text() or "").lower()
                if "website" in text or "visit" in text or len(href) > 0:
                    website = href
                    break

        if not website:
            # try meta tags
            meta = psoup.find("a", class_=lambda c: c and "website" in c)
            if meta and meta.get("href"):
                website = meta.get("href")

        if not website:
            # fallback: attempt to extract domain-looking urls from text
            texts = pr.text.split()
            for tok in texts:
                if tok.startswith("http") and "swissbiotech.org" not in tok:
                    website = tok.strip("\",.;)")
                    break

        if not website:
            continue

        domain = _extract_domain(website)
        if not domain or domain in seen_domains:
            continue
        # skip if already in sites.json
        if domain in existing_domains:
            continue
        # skip if cached recently
        if _is_cached_recent(cache, domain):
            continue
        seen_domains.add(domain)

        # attempt to extract a location string from the profile page
        loc_text = None
        # common selectors: address, .location, .company-location
        candidates = []
        h_addr = psoup.find("address")
        if h_addr:
            candidates.append(h_addr.get_text(" ", strip=True))
        for sel in (".location", ".company-location", ".address", ".company-address"):
            el = psoup.select_one(sel)
            if el:
                candidates.append(el.get_text(" ", strip=True))
        # fallback: look for lines mentioning cantons/cities
        text = pr.text
        for line in text.splitlines():
            l = line.strip()
            if "laus" in l.lower() or "vaud" in l.lower() or "genev" in l.lower() or "bern" in l.lower():
                candidates.append(l)
        if candidates:
            loc_text = next((c for c in candidates if c), None)

        career = find_career_page_for_domain(domain)
        name = psoup.find("h1")
        name_text = name.get_text().strip() if name else domain

        # geocode location (best-effort) and compute distance to Lausanne
        lat = lon = None
        distance_km = None
        if loc_text:
            try:
                ge = geocode_location(loc_text)
                if ge:
                    lat = float(ge.get("lat"))
                    lon = float(ge.get("lon"))
                    distance_km = haversine_km(46.5191, 6.6323, lat, lon)
            except Exception:
                lat = lon = None

        results.append({
            "name": name_text,
            "domain": domain,
            "profile_url": pu,
            "career_url": career,
            "location": loc_text,
            "lat": lat,
            "lon": lon,
            "distance_km": round(distance_km, 1) if distance_km is not None else None,
            "ats": detect_ats(career),
            "source": "swissbiotech",
        })
        # update cache so we don't try this domain again soon
        try:
            _update_cache_entry(cache, domain, {"profile_url": pu, "career_url": career})
        except Exception:
            pass
        count += 1
        time.sleep(0.2)

    return results


def geocode_location(query):
    """Best-effort geocoding using Nominatim (OpenStreetMap). Returns first match or None."""
    if not query:
        return None
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": query + ", Switzerland", "format": "json", "limit": 1}
    headers = {"User-Agent": "JobScanner/1.0 (geocoder)"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        return data[0]
    except Exception:
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    # convert degrees to radians
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 6371.0 * c


def detect_ats(url):
    """Best-effort detection of common ATS/platforms from a URL or page content.

    Returns a string with the detected ATS name or None.
    """
    if not url:
        return None
    lower = url.lower()
    # quick checks on URL/domain
    patterns = {
        "workable": "workable",
        "smartrecruiters": "smartrecruiters",
        "greenhouse": "greenhouse",
        "lever": "lever",
        "breezy": "breezyhr",
        "jobvite": "jobvite",
        "icims": "icims",
        "workday": "workday",
        "ashby": "ashby",
    }
    for name, token in patterns.items():
        if token in lower:
            return name

    # fallback: fetch the page and look for vendor strings
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        txt = (r.text or "").lower()
        for name, token in patterns.items():
            if token in txt:
                return name
    except Exception:
        return None
    return None


if __name__ == "__main__":
    # simple demo when run manually
    res = discover_pipeline(country="CH", query="machine learning", max_results=10)
    for r in res:
        print(r)
