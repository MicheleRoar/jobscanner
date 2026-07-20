"""
scraper.py
Crawls the "careers" pages of the sites listed in sites.json, looking for
links that look like job postings relevant to Michele's profile
(ML / LLM / computer vision / bioinformatics), and keeps track of which
ones are new since the last scan.

Honest disclaimer: every site does its own thing. The heuristic below
(keywords + link text length) works reasonably well on most classic
careers portals, but on some unusual sites it may find nothing, or a few
false positives. If a site stops giving sensible results, this is the
first place to check.
"""

import json
import multiprocessing
import os
import re
import signal
import smtplib
import tempfile
import time
import urllib.request
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, unquote
import hashlib

from playwright.sync_api import sync_playwright

# sites.json in the project folder is the "factory" list (the one that
# ships with the code). The real, dashboard-editable copy lives in
# data/sites.json — which is on the persistent volume, so it survives
# container restarts/rebuilds.
SITES_SEED_FILE = os.path.join(os.path.dirname(__file__), "sites.json")
SITES_FILE = os.path.join(os.path.dirname(__file__), "data", "sites.json")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "jobs_data.json")

# Keywords that indicate a position relevant to Michele's profile.
# Multilingual (EN/FR/IT) because sites in Romandy are often in French.
KEYWORDS = [
    "machine learning", "deep learning", "artificial intelligence", " ai ",
    "llm", "large language model", "computer vision", "nlp",
    "bioinformatic", "bioinformatique", "bioinformatician", "computational biology",
    "computational biologist", "bioinformatician / computational biologist",
    "biologie computationnelle", "data scientist", "data science",
    "data engineer", "ml engineer", "ai engineer", "research scientist",
    "research engineer", "postdoc", "post-doctoral", "postdoctoral",
    "phd", "python", "pytorch", "genomic", "genomique", "multimodal",
    "ml application",
    "neural network", "researcher", "chercheur", "ingénieur logiciel",
    "software engineer", "informaticien",
]

# Links/text too generic to keep even if they contain a keyword.
NOISE_PATTERNS = ["cookie", "privacy", "newsletter", "linkedin.com", "twitter.com", "facebook.com"]

# How many days after a spontaneous application to suggest a follow-up.
FOLLOWUP_DAYS = 21


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "sito"


def _ensure_sites_file():
    if not os.path.exists(SITES_FILE):
        os.makedirs(os.path.dirname(SITES_FILE), exist_ok=True)
        with open(SITES_SEED_FILE, encoding="utf-8") as f:
            seed = json.load(f)
        with open(SITES_FILE, "w", encoding="utf-8") as f:
            json.dump(seed, f, indent=2, ensure_ascii=False)


def load_sites():
    _ensure_sites_file()
    with open(SITES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_sites(sites):
    _ensure_sites_file()
    with open(SITES_FILE, "w", encoding="utf-8") as f:
        json.dump(sites, f, indent=2, ensure_ascii=False)


def add_site(name, url, scan=True, curated=None):
    sites = load_sites()
    existing_ids = {s["id"] for s in sites}
    base_id = slugify(name)
    new_id = base_id
    i = 2
    while new_id in existing_ids:
        new_id = f"{base_id}-{i}"
        i += 1
    site_rec = {"id": new_id, "name": name, "url": url, "scan": scan}
    if curated:
        site_rec["curated"] = curated
    sites.append(site_rec)
    save_sites(sites)
    return new_id


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"jobs": {}, "last_run": None}


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# short, ambiguous keywords: only match as a whole word, otherwise
# "llm" catches "Order FuLLMent", "ai" catches "trainer", etc.
WHOLE_WORD_KEYWORDS = {"llm", "nlp", "phd", "ai"}


def looks_like_job_link(text, href):
    if not text or not href:
        return False
    t = text.strip().lower()
    if len(t) < 4 or len(t) > 140:
        return False
    if any(n in href.lower() for n in NOISE_PATTERNS):
        return False
    for k in KEYWORDS:
        ks = k.strip()
        if ks in WHOLE_WORD_KEYWORDS:
            if re.search(rf"\b{ks}\b", t):
                return True
        elif k in t:
            return True
    return False


TRACKING_PARAM_PREFIXES = ["utm_", "fbclid", "gclid", "mc_eid", "yclid", "igshid"]
TRACKING_PARAM_NAMES = ["sessionid", "phpsessid", "jsessionid", "ref", "_s.crb"]


def normalize_url(url):
    """Normalizes a URL for deduplication:
    - strips fragments
    - percent-decodes the relevant components
    - strips tracking query params (utm_*, fbclid, etc.)
    - sorts the remaining params
    - strips inconsistent trailing slashes
    """
    try:
        p = urlparse(url)
    except Exception:
        return url

    # decode path and netloc
    path = unquote(p.path or "")
    netloc = p.netloc.lower()

    # filter query params
    qsl = parse_qsl(p.query, keep_blank_values=True)
    kept = []
    for k, v in qsl:
        lowk = k.lower()
        if any(lowk.startswith(pref) for pref in TRACKING_PARAM_PREFIXES):
            continue
        if lowk in TRACKING_PARAM_NAMES:
            continue
        kept.append((k, v))

    # sort params for stable representation
    kept.sort()
    query = urlencode(kept, doseq=True)

    # rebuild without fragment
    cleaned = urlunparse((p.scheme.lower(), netloc, path.rstrip('/'), '', query, ''))
    return cleaned


def fingerprint_url_and_title(url, title):
    # use normalized url and a simplified title to compute a stable fingerprint
    norm = normalize_url(url)
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    h = hashlib.sha256()
    h.update(norm.encode('utf-8'))
    h.update(b'|')
    h.update(t.encode('utf-8'))
    return h.hexdigest()


def _extract_links(page, base_url):
    # many sites embed job postings in an iframe of their ATS
    # (e.g. Ashby on neuralconcept.com): links must also be looked up inside
    # frames, resolving relative links against the URL of the frame that
    # contains them. Extraction happens with ONE evaluate call per frame:
    # reading text and href element by element is one IPC round-trip each,
    # and on portals with hundreds of links (Google, Meta) that made scans
    # take hours.
    links = []
    for frame in page.frames:
        frame_base = frame.url if frame.url and frame.url != "about:blank" else base_url
        try:
            raw = frame.evaluate(
                "() => Array.from(document.querySelectorAll('a'))"
                ".map(a => [a.innerText || '', a.getAttribute('href') || ''])"
            )
        except Exception:
            continue
        for text, href in raw:
            text = (text or "").strip()
            if not href:
                continue
            # some job cards put title + description inside the same <a>:
            # the title is the first line, the rest would get it rejected
            # for length (e.g. foreai.co)
            if "\n" in text:
                text = next((l.strip() for l in text.splitlines() if l.strip()), text)
            full_url = href if href.startswith("http") else urljoin(frame_base, href)
            links.append((text, full_url))
    return links


def _is_next_page_link(text, href):
    text_norm = (text or "").strip().lower()
    href_norm = (href or "").lower()
    if "next" in text_norm or "suiv" in text_norm:
        return True
    # URL tokens only count if followed by a digit: "page-milestones"
    # or "package=" are not pagination (on acimmune.com this made the
    # scraper wander off into the investor relations pages)
    if re.search(r"(?:^|[?&#/_-])(?:page=|p_page=|start=|offset=|page[/-])\d", href_norm):
        return True
    return False


MAX_PAGES = 15
# max time (seconds) to spend on a single site, pagination included:
# mega-portals (Google, Meta) have very slow pages and thousands of
# postings — without a cap a single scan could take hours
SITE_TIME_BUDGET_S = 240


def _find_next_page_action(page):
    """Looks for the "next page" control. Returns:
    - ("url", href) if it's a real link to follow with goto()
    - ("click", element_handle) if it's a JS/AJAX control (typical of
      SuccessFactors portals, which paginate via JS calls without changing
      the URL: in that case href is "javascript:void(0)" and it must be
      clicked, then wait for the content to update in place)
    - None if there is no next page (or the control is disabled)
    """
    # a single evaluate call to read the metadata of all links (see
    # _extract_links: per-element reads are too slow on large portals);
    # the element handle is only used for the candidate to click
    try:
        raw = page.evaluate(
            """() => Array.from(document.querySelectorAll('a')).map(a => ({
                text: a.innerText || '',
                href: a.getAttribute('href') || '',
                aria: a.getAttribute('aria-label') || '',
                title: a.getAttribute('title') || '',
                cls: a.getAttribute('class') || '',
                ariaDisabled: a.getAttribute('aria-disabled') || '',
                parentCls: a.parentElement ? (a.parentElement.getAttribute('class') || '') : '',
            }))"""
        )
    except Exception:
        return None

    for idx, a in enumerate(raw):
        text = (a["text"] or "").strip()
        href = a["href"] or ""
        aria = (a["aria"] or "").strip().lower()
        title_attr = (a["title"] or "").strip().lower()
        cls = (a["cls"] or "").lower()
        parent_class = (a["parentCls"] or "").lower()

        is_candidate = "next page" in aria or "next page" in title_attr or _is_next_page_link(text, href)
        if not is_candidate:
            continue

        if "disabled" in cls or "disabled" in parent_class or a["ariaDisabled"] == "true":
            return None

        if href.startswith("http"):
            # the next page is always on the same host: a link to a
            # different domain/subdomain is a false positive of the heuristic
            if urlparse(href).netloc.lower() != urlparse(page.url).netloc.lower():
                continue
            return ("url", href)
        try:
            return ("click", page.query_selector_all("a")[idx])
        except Exception:
            return None

    return None


WORKABLE_ACCOUNT_RE = re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")


def _scan_workable(site):
    """apply.workable.com pages render job postings as clickable cards
    without <a> tags, so link extraction sees nothing. The widget's public
    API does expose the title and URL of every posting, though."""
    account = WORKABLE_ACCOUNT_RE.search(site["url"]).group(1)
    api = f"https://apply.workable.com/api/v1/widget/accounts/{account}"
    req = urllib.request.Request(api, headers={"User-Agent": "JobMonitorBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    postings = []
    for job in payload.get("jobs", []):
        title = (job.get("title") or "").strip()
        url = job.get("url") or job.get("shortlink") or site["url"]
        if looks_like_job_link(title, url):
            postings.append({"title": title, "url": url})
    return postings


SMARTRECRUITERS_COMPANY_RE = re.compile(r"smartrecruiters\.com/([A-Za-z0-9_-]+)")


def _scan_smartrecruiters(site):
    """SmartRecruiters portals (e.g. Avaloq) block the headless browser
    with a 403, but they expose a paginated public API with all postings."""
    company = SMARTRECRUITERS_COMPANY_RE.search(site["url"]).group(1)
    postings = []
    offset = 0
    while True:
        api = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset={offset}"
        req = urllib.request.Request(api, headers={"User-Agent": "JobMonitorBot/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
        items = payload.get("content", [])
        if not items:
            break
        for job in items:
            title = (job.get("name") or "").strip()
            url = f"https://jobs.smartrecruiters.com/{company}/{job.get('id')}"
            if looks_like_job_link(title, url):
                postings.append({"title": title, "url": url})
        offset += len(items)
        if offset >= payload.get("totalFound", 0):
            break
    return postings


def _goto(page, url):
    """Navigates with domcontentloaded (fast and reliable) and then allows
    up to 8s for the network to settle. Waiting for networkidle as the
    goto condition seemed cleaner, but sites that never go "idle"
    (analytics, websockets) paid a 30s timeout on EVERY page of the
    pagination, and a full scan would take hours."""
    page.goto(url, timeout=30000, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass  # the site never settles down: proceed with what we have
    page.wait_for_timeout(1500)


def _links_signature(page):
    """Signature of the page's content based on all extracted hrefs.
    Used to tell whether the content actually changed after an AJAX
    pagination click (SuccessFactors-style portals don't change the URL)."""
    return frozenset(u for _, u in _extract_links(page, page.url))


def _wait_for_content_change(page, before_signature, timeout_ms=12000, poll_ms=500):
    """After an AJAX pagination click, waits until the page's links change,
    instead of a fixed wait: on slow connections/portals 2 seconds wasn't
    enough and the scan would stop at the first page."""
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
        if _links_signature(page) != before_signature:
            # small extra margin for the list to finish rendering
            page.wait_for_timeout(700)
            return True
    return False


def _dedupe_postings(postings):
    seen = set()
    unique = []
    for p_ in postings:
        key = (p_["title"], p_["url"])
        if key not in seen:
            seen.add(key)
            unique.append(p_)
    return unique


def scan_site(site):
    """Returns (postings, error). postings is a list of {title, url},
    error is either None or a string with the error message.
    Never raises exceptions to the caller: a broken site must not
    stop the others."""
    postings = []
    error = None
    try:
        if "apply.workable.com/" in site["url"]:
            postings = _scan_workable(site)
            return _dedupe_postings(postings), None
        if "smartrecruiters.com/" in site["url"]:
            postings = _scan_smartrecruiters(site)
            return _dedupe_postings(postings), None
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                user_agent="Mozilla/5.0 (compatible; JobMonitorBot/1.0; contact: micheleleone@outlook.com)"
            )
            # Playwright's default is 30s for EVERY element operation
            # (inner_text, click...): on pages with hundreds of unstable
            # links, extraction can take hours. 5s is enough, and the
            # exception on a single link is already handled by skipping it.
            page.set_default_timeout(5000)
            _goto(page, site["url"])

            visited_urls = {site["url"]}
            prev_signature = None
            started_at = time.monotonic()
            for _ in range(MAX_PAGES):
                current_links = [
                    (text, full_url)
                    for text, full_url in _extract_links(page, page.url)
                    if looks_like_job_link(text, full_url)
                ]
                # if the "next" page shows exactly the same postings as
                # before, the click/next didn't really advance: stop here
                # to avoid an infinite loop instead of duplicating results.
                signature = frozenset(full_url for _, full_url in current_links)
                if signature and signature == prev_signature:
                    break
                prev_signature = signature

                postings.extend({"title": text.strip(), "url": full_url} for text, full_url in current_links)

                if time.monotonic() - started_at > SITE_TIME_BUDGET_S:
                    print(f"[{site['name']}] time budget exhausted, stopping at this page")
                    break

                next_action = _find_next_page_action(page)
                if next_action is None:
                    break

                kind, value = next_action
                if kind == "url":
                    if value in visited_urls:
                        break
                    visited_urls.add(value)
                    _goto(page, value)
                else:
                    before = _links_signature(page)
                    try:
                        value.click()
                    except Exception:
                        break
                    if not _wait_for_content_change(page, before):
                        # the click produced nothing new: end of pages
                        break

            browser.close()
    except Exception as e:
        error = str(e).split("\n")[0]
        print(f"[{site['name']}] error during scan: {e}")

    return _dedupe_postings(postings), error


# margin over the internal budget before killing the site's process
SITE_HARD_TIMEOUT_S = SITE_TIME_BUDGET_S + 120


def _scan_site_worker(site, result_path):
    # new process group: the browser and playwright driver launched from
    # here are part of it, so a killpg from the parent wipes out the whole
    # tree and doesn't leave orphaned Chromium processes eating memory
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    postings, error = scan_site(site)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({"postings": postings, "error": error}, f)


def scan_site_with_timeout(site, timeout_s=SITE_HARD_TIMEOUT_S):
    """Runs scan_site in a separate process with a hard timeout.
    The internal timeouts (goto, element actions, time budget) cover
    almost everything, but page.evaluate is not interruptible: a page with
    an infinite JS loop can hang the thread for hours (happened with sites
    on Vercel on 2026-07-17). A process can always be killed.
    The result travels through a temp file and the wait uses join(timeout):
    no multiprocessing.Queue, whose get(timeout) proved capable of
    blocking past the timeout (feeder thread + poll)."""
    ctx = multiprocessing.get_context("spawn")
    fd, result_path = tempfile.mkstemp(prefix="scan_", suffix=".json")
    os.close(fd)
    try:
        proc = ctx.Process(target=_scan_site_worker, args=(site, result_path), daemon=True)
        proc.start()
        proc.join(timeout_s)
        if proc.is_alive():
            # SIGKILL the whole process group: SIGTERM may not be enough
            # when playwright is wedged, and killing only the child would
            # leave its Chromium processes behind
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.join(10)
            return [], f"scan got stuck, aborted after {timeout_s}s"
        try:
            with open(result_path, encoding="utf-8") as f:
                payload = json.load(f)
            return payload["postings"], payload["error"]
        except Exception:
            return [], "the scan process died without producing a result"
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass


def send_email(new_postings):
    host = os.environ.get("SMTP_HOST")
    to_addr = os.environ.get("EMAIL_TO")
    if not host or not to_addr:
        print("SMTP not configured (SMTP_HOST/EMAIL_TO missing): skipping email.")
        return

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    sender = os.environ.get("EMAIL_FROM", user or to_addr)

    body_lines = [f"- [{p['site']}] {p['title']}\n  {p['url']}" for p in new_postings]
    body = "New postings found by the monitor:\n\n" + "\n\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = f"[Job Monitor] {len(new_postings)} new postings found"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            if user and pwd:
                server.login(user, pwd)
            server.sendmail(sender, [to_addr], msg.as_string())
        print(f"Email sent with {len(new_postings)} new postings.")
    except Exception as e:
        print(f"Error sending email: {e}")


def _merge_job_records(records):
    """Merges multiple records that represent the same posting (same key
    after normalization), preserving the useful state (application status,
    notes, oldest first-seen date) instead of discarding it at random."""
    records = sorted(records, key=lambda r: r.get("first_seen") or "")
    base = dict(records[0])
    for r in records[1:]:
        if r.get("first_seen") and (not base.get("first_seen") or r["first_seen"] < base["first_seen"]):
            base["first_seen"] = r["first_seen"]
        base["active"] = base.get("active", False) or r.get("active", False)
        if r.get("applied") and not base.get("applied"):
            base["applied"] = True
            base["applied_date"] = r.get("applied_date")
        base["dismissed"] = base.get("dismissed", False) or r.get("dismissed", False)
        base["favorite"] = base.get("favorite", False) or r.get("favorite", False)
        if not base.get("note") and r.get("note"):
            base["note"] = r.get("note")
    return base


def dedupe_jobs(data):
    """Recomputes each posting's key with the current normalize_url and
    merges duplicates saved in the past under different keys (e.g. because
    of URLs not correctly normalized by earlier versions of the scraper).
    Must be re-run on every scan so the data stays clean even as new cases
    of imperfect normalization surface."""
    groups = {}
    order = []
    for old_key, job in data["jobs"].items():
        site_id = job.get("site_id") or old_key.split("::", 1)[0]
        normalized = normalize_url(job.get("url", ""))
        new_key = f"{site_id}::{normalized}"
        job["normalized_url"] = normalized
        job["fingerprint"] = fingerprint_url_and_title(normalized, job.get("title", ""))
        if new_key not in groups:
            groups[new_key] = []
            order.append(new_key)
        groups[new_key].append(job)

    data["jobs"] = {new_key: _merge_job_records(groups[new_key]) for new_key in order}
    return data


def run_scan():
    print(f"[{date.today().isoformat()}] Starting scan...")
    sites = load_sites()
    data = load_data()
    data = dedupe_jobs(data)
    today = date.today().isoformat()

    # mark everything as "not found again yet" this round, so we can
    # detect postings that disappeared (probably closed)
    for job in data["jobs"].values():
        job["active"] = False

    if "site_status" not in data:
        data["site_status"] = {}

    new_postings = []
    for site in sites:
        if not site.get("scan", True):
            continue
        t0 = time.monotonic()
        found, error = scan_site_with_timeout(site)
        elapsed = time.monotonic() - t0
        print(f"  {site['name']}: {len(found)} relevant postings found in {elapsed:.0f}s"
              + (f" (error: {error})" if error else ""), flush=True)

        data["site_status"][site["id"]] = {
            "name": site["name"],
            "url": site["url"],
            "count": len(found),
            "error": error,
            "checked_at": today,
        }

        for f in found:
            normalized = normalize_url(f.get('url', ''))
            fp = fingerprint_url_and_title(normalized, f.get('title', ''))
            key = f"{site['id']}::{normalized}"
            if key not in data["jobs"]:
                data["jobs"][key] = {
                    "site": site["name"],
                    "site_id": site["id"],
                    "title": f["title"],
                    "url": f["url"],
                    "normalized_url": normalized,
                    "fingerprint": fp,
                    "first_seen": today,
                    "active": True,
                    "applied": False,
                    "applied_date": None,
                    "dismissed": False,
                    "favorite": False,
                    "note": "",
                }
                new_postings.append(data["jobs"][key])
            else:
                data["jobs"][key]["active"] = True

    data["last_run"] = today
    save_data(data)

    if new_postings:
        send_email(new_postings)
    print(f"[{date.today().isoformat()}] Scan completed. {len(new_postings)} new postings.")
    return new_postings


def toggle_job_applied(job_key):
    data = load_data()
    job = data["jobs"].get(job_key)
    if job is None:
        return
    job["applied"] = not job.get("applied", False)
    job["applied_date"] = date.today().isoformat() if job["applied"] else None
    save_data(data)


def toggle_job_favorite(job_key):
    data = load_data()
    job = data["jobs"].get(job_key)
    if job is None:
        return
    job["favorite"] = not job.get("favorite", False)
    save_data(data)


def toggle_job_dismissed(job_key):
    data = load_data()
    job = data["jobs"].get(job_key)
    if job is None:
        return
    job["dismissed"] = not job.get("dismissed", False)
    save_data(data)


def save_job_note(job_key, note_text):
    data = load_data()
    job = data["jobs"].get(job_key)
    if job is None:
        return
    job["note"] = note_text
    save_data(data)


SPONTANEOUS_STATUSES = ["to_apply", "applied", "responded"]


def set_spontaneous_status(site_id, status):
    if status not in SPONTANEOUS_STATUSES:
        return
    data = load_data()
    if "spontaneous_status" not in data:
        data["spontaneous_status"] = {}
    rec = data["spontaneous_status"].get(site_id, {"status": "to_apply", "applied_date": None, "note": ""})

    if status == "applied" and rec.get("status") != "applied":
        rec["applied_date"] = date.today().isoformat()
    if status == "to_apply":
        rec["applied_date"] = None

    rec["status"] = status
    data["spontaneous_status"][site_id] = rec
    save_data(data)


def save_spontaneous_note(site_id, note_text):
    data = load_data()
    if "spontaneous_status" not in data:
        data["spontaneous_status"] = {}
    rec = data["spontaneous_status"].get(site_id, {"status": "to_apply", "applied_date": None, "note": ""})
    rec["note"] = note_text
    data["spontaneous_status"][site_id] = rec
    save_data(data)


def needs_followup(rec):
    """True if a spontaneous application has been sitting without a
    response for too long."""
    if not rec or rec.get("status") != "applied" or not rec.get("applied_date"):
        return False, 0
    applied = date.fromisoformat(rec["applied_date"])
    days = (date.today() - applied).days
    return days >= FOLLOWUP_DAYS, days


if __name__ == "__main__":
    run_scan()
