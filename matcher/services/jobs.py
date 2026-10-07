"""Fetch live job postings from company career sites, newest first.

Most company career pages are rendered by an applicant-tracking system (ATS). Greenhouse, Lever, Ashby and
Workable publish public JSON feeds for every board, so we read those directly. Any other careers page
can be added as `ats: jsonld` - we then read the schema.org JobPosting data that most career pages
embed for Google Jobs.
"""
import concurrent.futures as cf
import html
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from django.conf import settings
from django.core.cache import cache

from .catalog import enrich_job

log = logging.getLogger(__name__)
UA = {"User-Agent": "ResumeJobMatcher/1.0 (+career-feed reader)"}
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
TAG = re.compile(r"<[^>]+>")


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"</(p|div|li|h\d|br)>|<br\s*/?>", "\n", s, flags=re.I)
    s = TAG.sub(" ", html.unescape(s))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def _ts(value):
    """Best-effort conversion of ISO strings / epoch ms to an aware datetime."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError):
        return None


def _job(company, title, url, location, posted, description, detail=None):
    return enrich_job({
        "detail": detail,
        "company": company,
        "title": (title or "").strip(),
        "url": url,
        "location": (location or "").strip(),
        "posted": posted.isoformat() if posted else "",
        "description": (description or "")[:15000],
    })



def _norm_source(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedupe(jobs):
    """Merge duplicate postings from employer ATS and official park feeds."""
    best = {}
    for j in jobs:
        key = (_norm_source(j.get("title")), _norm_source(j.get("company")), _norm_source((j.get("location") or "").split(",")[0]))
        cur = best.get(key)
        j.setdefault("sources", [])
        if j.get("source") and j["source"] not in j["sources"]:
            j["sources"].append(j["source"])
        if cur is None:
            best[key] = j
            continue
        for src in j.get("sources", []):
            if src not in cur["sources"]:
                cur["sources"].append(src)
        if len(j.get("description") or "") > len(cur.get("description") or ""):
            j["sources"] = cur["sources"]
            best[key] = j
    return list(best.values())

# ---------- per-ATS readers ----------

def _greenhouse(c, client):
    # The list WITHOUT content is tiny and fast; full text is fetched later only for relevant jobs.
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs")
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield _job(c["name"], j.get("title"), j.get("absolute_url"),
                   (j.get("location") or {}).get("name"),
                   _ts(j.get("first_published") or j.get("updated_at")), "",
                   detail={"ats": "greenhouse", "token": c["token"], "id": j.get("id")})


def _lever(c, client):
    r = client.get(f"https://api.lever.co/v0/postings/{c['token']}", params={"mode": "json"})
    r.raise_for_status()
    for j in r.json():
        extra = " ".join(f"{l.get('text', '')}\n{strip_html(l.get('content'))}" for l in j.get("lists", []))
        desc = f"{j.get('descriptionPlain', '')}\n{extra}\n{j.get('additionalPlain', '')}"
        yield _job(c["name"], j.get("text"), j.get("hostedUrl"),
                   (j.get("categories") or {}).get("location"), _ts(j.get("createdAt")), desc)


def _ashby(c, client):
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{c['token']}")
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        yield _job(c["name"], j.get("title"), j.get("jobUrl"), j.get("location"),
                   _ts(j.get("publishedAt")), j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")))


def _workable(c, client):
    # Public widget feed for one company account; details=true includes the full description in the same call.
    r = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{c['token']}", params={"details": "true"})
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        place = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        if j.get("telecommuting") and not place:
            place = "Remote"
        yield _job(c["name"], j.get("title"), j.get("url") or j.get("shortlink") or j.get("application_url"),
                   place, _ts(j.get("published_on") or j.get("created_at")), strip_html(j.get("description")))


def _walk_jobposting(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jobposting(n)
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            yield node
        for v in node.values():
            if isinstance(v, (list, dict)):
                yield from _walk_jobposting(v)


def _jsonld(c, client):
    r = client.get(c["url"])
    r.raise_for_status()
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.text, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for p in _walk_jobposting(data):
            loc = p.get("jobLocation")
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
            place = ", ".join(x for x in [addr.get("addressLocality"), addr.get("addressCountry")] if isinstance(x, str))
            yield _job(c["name"], p.get("title"), urljoin(c["url"], p.get("url") or c["url"]), place,
                       _ts(p.get("datePosted")), strip_html(p.get("description")))


READERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "workable": _workable, "jsonld": _jsonld}


def _park_detail_text(url):
    """Official IT-park listing pages have no JSON API; read the visible text of the detail page."""
    from bs4 import BeautifulSoup
    from .netutil import SafeClient
    with SafeClient(settings.JOB_FETCH_TIMEOUT, UA) as client:
        r = client.get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:15000]


def fetch_detail(job):
    """Fill in job['description'] for lightweight listings (cached). Never raises."""
    d = job.get("detail")
    if not d or job.get("description"):
        return job
    from . import snapshot
    if snapshot.enabled():                                   # descriptions were already filled in by the scraper
        return job
    if "ats" not in d:                                          # official park listing (e.g. Infopark)
        url = d.get("url") or job.get("url")
        key = f"parkdetail:{url}"
        text = cache.get(key)
        if text is None:
            try:
                text = _park_detail_text(url) if url else ""
                cache.set(key, text, settings.JOB_CACHE_SECONDS * 4)
            except Exception as exc:
                log.warning("park detail fetch failed: %s", exc)
                text = ""
        job["description"] = text
        return enrich_job(job)
    key = f"jobdetail:{d['ats']}:{d['token']}:{d['id']}"
    text = cache.get(key)
    if text is None:
        try:
            with httpx.Client(timeout=settings.JOB_FETCH_TIMEOUT, headers=UA, follow_redirects=True) as client:
                r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{d['token']}/jobs/{d['id']}")
                r.raise_for_status()
                text = strip_html(r.json().get("content"))[:15000]
            cache.set(key, text, settings.JOB_CACHE_SECONDS * 4)
        except Exception as exc:
            log.warning("detail fetch failed: %s", exc)
            text = ""
    job["description"] = text
    return enrich_job(job)


def fetch_details(jobs, workers=24):
    todo = [j for j in jobs if j.get("detail") and not j.get("description")]
    if todo:
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(fetch_detail, todo))
    return jobs


# ---------- orchestration ----------

def load_companies():
    path = settings.COMPANIES_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=_Loader) or {}
    except FileNotFoundError:
        return []
    out = []
    for c in data.get("companies", []):
        if c.get("ats") in READERS and c.get("name") and (c.get("token") or c.get("url")):
            if c["ats"] == "jsonld" and urlparse(c["url"]).scheme not in ("http", "https"):
                continue
            out.append(c)
    return out


# ---- source cache: fresh -> instant; stale -> instant + refreshed in the background; empty -> remembered longer ----
_refreshing = set()
_refresh_lock = threading.Lock()
_refresh_pool = cf.ThreadPoolExecutor(max_workers=6, thread_name_prefix="job-refresh")


def _source_key(c):
    return f"jobs2:{c['ats']}:{c.get('token') or c.get('url')}"


def _fetch_live(c):
    """Hit the career site now and store the result (or the failure). Returns (jobs, error)."""
    key = _source_key(c)
    timeout = httpx.Timeout(settings.JOB_FETCH_TIMEOUT, connect=min(4.0, settings.JOB_FETCH_TIMEOUT))
    try:
        with httpx.Client(timeout=timeout, headers=UA, follow_redirects=True) as client:
            jobs = [j for j in READERS[c["ats"]](c, client) if j["title"] and j["url"]]
    except Exception as exc:                                    # one dead site must not break the search
        log.warning("job fetch failed for %s: %s", c["name"], exc)
        err = f"{c['name']}: {type(exc).__name__}"
        cache.set(key + ":err", err, getattr(settings, "JOB_ERROR_CACHE_SECONDS", 3600))
        return [], err
    cache.delete(key + ":err")
    cache.set(key, {"jobs": jobs, "at": time.time()}, getattr(settings, "JOB_STALE_SECONDS", 86400))
    return jobs, None


def _refresh_in_background(c):
    key = _source_key(c)
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run():
        try:
            _fetch_live(c)
        finally:
            with _refresh_lock:
                _refreshing.discard(key)
    _refresh_pool.submit(run)


def fetch_company(c, refresh=False):
    """Return (jobs, error).

    Fresh cache -> returned instantly. Expired cache -> the OLD list is returned instantly and a background refresh
    is started (stale-while-revalidate), so a search never waits for a site it has seen before. Dead sources and
    sites with no job data are remembered (JOB_ERROR_CACHE_SECONDS / JOB_EMPTY_CACHE_SECONDS) instead of being
    retried in every search. refresh=True (used by the warmer) re-fetches expired sources synchronously.
    """
    key = _source_key(c)
    entry = cache.get(key)
    if isinstance(entry, dict) and "jobs" in entry:
        ttl = settings.JOB_CACHE_SECONDS if entry["jobs"] else getattr(settings, "JOB_EMPTY_CACHE_SECONDS", 21600)
        if time.time() - entry["at"] < ttl:
            return entry["jobs"], None
        if not refresh:
            _refresh_in_background(c)
            return entry["jobs"], None
    else:
        bad = cache.get(key + ":err")
        if bad:
            return [], bad
    return _fetch_live(c)


def fetch_all(companies=None, workers=None, progress=None, refresh=False):
    """Fetch every company in parallel. progress(done, total) is called as each one finishes.

    Hard overall deadline (JOB_SEARCH_DEADLINE seconds): sources still running when it passes are reported as
    timed out instead of making the whole search wait for the slowest site.
    """
    from . import snapshot
    if snapshot.enabled() and companies is None:           # PythonAnywhere free: never scrape, read the GitHub-built file
        data = snapshot.load()
        if progress:
            progress(data.get("n_companies", 0), data.get("n_companies", 0))
        return snapshot.jobs_copy(), list(data.get("errors", [])), data.get("n_companies", 0)
    companies = companies if companies is not None else load_companies()
    workers = workers or getattr(settings, "JOB_FETCH_WORKERS", 48)
    deadline = getattr(settings, "JOB_SEARCH_DEADLINE", 60)
    jobs, errors = [], []
    total = len(companies)
    if progress:
        progress(0, total)
    pool = cf.ThreadPoolExecutor(max_workers=workers)
    futures = {(pool.submit(fetch_company, c, True) if refresh else pool.submit(fetch_company, c)): c for c in companies}
    done = 0
    try:
        for fut in cf.as_completed(futures, timeout=deadline):
            got, err = fut.result()
            jobs.extend(got)
            if err:
                errors.append(err)
            done += 1
            if progress:
                progress(done, total)
    except cf.TimeoutError:
        for fut, c in futures.items():
            if not fut.done():
                errors.append(f"{c['name']}: Timeout")
        if progress:
            progress(total, total)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return jobs, errors, total
