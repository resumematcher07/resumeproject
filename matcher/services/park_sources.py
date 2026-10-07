"""Official IT-park job listings (Infopark, Technopark, Cyberpark ...), read WITHOUT knowing their exact markup.

Park sites change their pages often and some load the list with JavaScript, so a single hard-coded parser silently
returns nothing (and an empty result used to be cached for hours).  Each park is now read with a ladder of strategies
and the first one that yields jobs wins:

  1. wp-json   - WordPress sites expose every custom post type (jobs / careers / vacancies) as JSON
  2. json-ld   - schema.org JobPosting blocks
  3. embedded  - JSON inside <script> tags (__NEXT_DATA__, window.__DATA__, application/json ...)
  4. html      - tables and cards, found by structure (a link to a job page + nearby text), not by CSS class
  5. api       - API / AJAX URLs mentioned in the page or its scripts
  6. render    - (optional) a real headless browser via Playwright; it also records the JSON the page loads itself

Pagination is followed.  No title filtering happens here: every listing is returned and the matcher scores them, so a
"UI/UX Designer" opening is never dropped by the reader.  Every attempt is recorded in a report that the results page
and `manage.py diagnose_parks` show, so a failure always says WHY.
"""
import concurrent.futures as cf
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache

from .netutil import SafeClient
from .jobs import UA, _job, _ts, strip_html, _walk_jobposting

log = logging.getLogger(__name__)

# Candidate listing URLs per park (tried in order until one yields jobs).
PARKS = {
    "infopark": {"name": "Infopark", "place": "Infopark, Kochi, Kerala",
                 "urls": ["https://infopark.in/companies-job", "https://infopark.in/companies/job-search"]},
    "technopark": {"name": "Technopark", "place": "Technopark, Thiruvananthapuram, Kerala",
                   "urls": ["https://technopark.in/job-search", "https://technopark.org/job-search"]},
    "cyberpark": {"name": "Cyberpark", "place": "Cyberpark, Kozhikode, Kerala",
                  "urls": ["https://cyberparks.in/careers/", "https://www.cyberparkkerala.org/careers/"]},
}
SOURCES = {k: v["urls"][0] for k, v in PARKS.items()}          # kept for older callers / README

BROWSER_HEADERS = {**UA, "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                   "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
MAX_PAGES = lambda: int(getattr(settings, "PARK_MAX_PAGES", 12))   # noqa: E731
JOBISH = re.compile(r"job|career|vacanc|opening|position|opportunit|recruit|hiring|requirement|details?|apply", re.I)
SKIP_TEXT = re.compile(r"^(home|about( us)?|contact( us)?|login|log in|register|sign ?in|sign ?up|read more|view all|view|more|next|prev(ious)?|"
                       r"apply( now)?|details?|view details?|click here|search|submit|reset|\d+|«|»|›|‹|…|\.\.\.)$", re.I)
DATE_BAD_CONTEXT = re.compile(r"clos|last date|expir|valid|deadline|till|until|end date|walk-?in", re.I)
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RX = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(rf"\b(\d{{1,2}})[ \-/.](?:({MONTHS})[a-z]*)[ \-/.,]+(\d{{2,4}})\b", re.I), "dmy_name"),
    (re.compile(rf"\b(?:({MONTHS})[a-z]*)\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "mdy_name"),
    (re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b"), "dmy_num"),
]
_MON = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}

TITLE_KEYS = ("title", "job_title", "jobtitle", "jobTitle", "designation", "position", "post_title", "post", "role", "vacancy",
              "job_name", "opening", "name")
COMPANY_KEYS = ("company", "company_name", "companyname", "companyName", "organization", "organisation", "employer", "org", "firm", "client")
URL_KEYS = ("url", "link", "permalink", "job_url", "jobUrl", "apply_url", "applyUrl", "details_url", "href", "guid")
DATE_KEYS = ("date_posted", "datePosted", "posted", "posted_on", "postedOn", "post_date", "created_at", "createdAt", "publish_date",
             "published", "start_date", "date", "modified")
LOC_KEYS = ("location", "job_location", "city", "place", "address")


# ---------- small helpers ----------

def _clean(s):
    return re.sub(r"\s+", " ", strip_html(str(s or ""))).strip()


def _parse_date(text, strict_context=True):
    """First plausible *posting* date in a string (closing dates are ignored). Also understands '3 days ago'."""
    text = text or ""
    low = text.lower()
    if "today" in low and "posted" in low:
        return datetime.now(timezone.utc)
    m = re.search(r"(\d+)\+?\s*(day|week|month|hour)s?\s+ago", low)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return datetime.now(timezone.utc) - timedelta(days={"day": n, "week": 7 * n, "month": 30 * n, "hour": 0}[unit])
    for rx, kind in DATE_RX:
        for m in rx.finditer(text):
            if strict_context and DATE_BAD_CONTEXT.search(text[max(0, m.start() - 22):m.start()]):
                continue
            try:
                if kind == "ymd":
                    y, mo, d = map(int, m.groups())
                elif kind == "dmy_name":
                    d, mo, y = int(m.group(1)), _MON[m.group(2).lower()[:3]], int(m.group(3))
                elif kind == "mdy_name":
                    mo, d, y = _MON[m.group(1).lower()[:3]], int(m.group(2)), int(m.group(3))
                else:
                    d, mo, y = map(int, m.groups())
                y += 2000 if y < 100 else 0
                return datetime(y, mo, d, tzinfo=timezone.utc)
            except (ValueError, KeyError):
                continue
    return None


def _looks_js_rendered(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    anchors = len(soup.find_all("a", href=True))
    marker = re.search(r'id="(root|app|__next|__nuxt)"|ng-app|data-reactroot|enable javascript|noscript', html, re.I)
    return (len(text) < 600 and anchors < 25) or (bool(marker) and len(text) < 1500)


def _mk(park, company, title, url, posted, description="", location=None):
    p = PARKS[park]
    company = _clean(company) or f"{p['name']} company"
    return _job(company, _clean(title), url, location or p["place"], posted, description,
                detail={"park_source": park, "url": url})


# ---------- strategy: generic JSON -> jobs ----------

def _pick(d, keys):
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = low.get(k.lower())
        if isinstance(v, dict):
            v = v.get("rendered") or v.get("name") or v.get("title") or v.get("text")
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v)
    return ""


def _json_lists(node, depth=0):
    """Yield every list of dicts inside a JSON document."""
    if depth > 7:
        return
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node):
            yield node
        for x in node[:50]:
            yield from _json_lists(x, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            if isinstance(v, (list, dict)):
                yield from _json_lists(v, depth + 1)


def jobs_from_json(data, park, base_url):
    out = []
    for rows in _json_lists(data):
        titled = [r for r in rows if _pick(r, TITLE_KEYS)]
        if len(titled) < max(1, len(rows) // 2):
            continue
        # a list of plain nav/menu items has titles but no job-like extra field - require url/company/date/description
        for r in titled:
            title = _clean(_pick(r, TITLE_KEYS))
            url = _pick(r, URL_KEYS)
            if url and not url.startswith(("http", "/")) and not url.startswith("#"):
                url = urljoin(base_url, url)
            elif url:
                url = urljoin(base_url, url)
            slug = _pick(r, ("slug",))
            rid = _pick(r, ("id", "job_id", "jobId", "ID"))
            if not url:
                if slug:
                    url = urljoin(base_url, f"{base_url.rstrip('/')}/{slug}")
                elif rid:
                    url = f"{base_url}{'&' if '?' in base_url else '?'}job={rid}"
                else:
                    continue
            extra = sum(bool(_pick(r, k)) for k in (COMPANY_KEYS, DATE_KEYS, ("content", "description", "excerpt", "summary", "job_description")))
            if extra == 0 and not slug and not rid:
                continue
            company = _pick(r, COMPANY_KEYS)
            if not company and isinstance(r.get("acf"), dict):
                company = _pick(r["acf"], COMPANY_KEYS)
            desc = _pick(r, ("content", "description", "job_description", "excerpt", "summary"))
            posted = _ts(_pick(r, DATE_KEYS)) or _parse_date(_pick(r, DATE_KEYS), False)
            out.append(_mk(park, company, title, url, posted, strip_html(desc)[:15000]))
    return out


def jobs_from_jsonld(html, park, base_url):
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for p in _walk_jobposting(data):
            org = p.get("hiringOrganization")
            org = org.get("name") if isinstance(org, dict) else org
            out.append(_mk(park, org, p.get("title"), urljoin(base_url, p.get("url") or base_url), _ts(p.get("datePosted")),
                           strip_html(p.get("description"))))
    return out


def jobs_from_embedded(html, park, base_url):
    out = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        body = m.group(1).strip()
        if len(body) < 40:
            continue
        cands = []
        if body[:1] in "[{":
            cands.append(body)
        for mm in re.finditer(r"(?:=|\()\s*(\{.{40,}\}|\[.{40,}\])\s*[;)]?\s*$", body, re.S):
            cands.append(mm.group(1))
        for c in cands[:3]:
            try:
                out.extend(jobs_from_json(json.loads(c), park, base_url))
            except ValueError:
                continue
    return out


# ---------- strategy: HTML structure ----------

def _in_chrome(tag):
    for p in tag.parents:
        if p.name in ("nav", "footer", "header", "aside"):
            return True
        cls = " ".join(p.get("class", []) if hasattr(p, "get") else []).lower()
        if re.search(r"\b(menu|navbar|nav|footer|breadcrumb|pagination|pager|sidebar|social)\b", cls):
            return True
    return False


def _row_jobs(soup, park, base_url):
    out = []
    for table in soup.find_all("table"):
        head = [_clean(c.get_text(" ")) for c in (table.find("tr").find_all(["th", "td"]) if table.find("tr") else [])]
        roles = {}
        for i, h in enumerate(head):
            hl = h.lower()
            if re.search(r"clos|last date|expir|deadline", hl):
                roles[i] = "closing"
            elif re.search(r"job|position|designation|role|title|post\b|vacancy|opening", hl) and "date" not in hl:
                roles.setdefault("title", i); roles[i] = "title"
            elif re.search(r"company|organi[sz]ation|employer|firm|name", hl):
                roles[i] = "company"
            elif re.search(r"date|posted|published", hl):
                roles[i] = "date"
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td"])
            if len(cells) < 2:
                continue
            texts = [_clean(c.get_text(" ")) for c in cells]
            links = [a for a in tr.find_all("a", href=True) if not a["href"].startswith(("#", "javascript", "mailto", "tel"))]
            title = next((texts[i] for i, r in roles.items() if r == "title" and i < len(texts) and texts[i]), "")
            company = next((texts[i] for i, r in roles.items() if r == "company" and i < len(texts) and texts[i]), "")
            dtext = next((texts[i] for i, r in roles.items() if r == "date" and i < len(texts)), "")
            if not title:                                               # headerless table: guess
                cand = [t for t in texts if t and not _parse_date(t, False) and not SKIP_TEXT.match(t) and len(t) > 2]
                if not cand:
                    continue
                title = cand[0]
                company = company or (cand[1] if len(cand) > 1 else "")
            if not links:
                continue
            anchor = next((a for a in links if _clean(a.get_text(" ")).lower() == title.lower()), None) or \
                next((a for a in links if JOBISH.search(a["href"])), None) or links[-1]
            posted = _parse_date(dtext, False) or _parse_date(" ".join(texts))
            out.append(_mk(park, company, title, urljoin(base_url, anchor["href"]), posted))
    return out


def _card_jobs(soup, park, base_url, listing_url):
    cands = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript", "mailto", "tel")) or _in_chrome(a):
            continue
        full = urljoin(base_url, href)
        if full.rstrip("/") == listing_url.rstrip("/") or re.search(r"[?&](page|paged|pg)=\d+|/page/\d+", full):
            continue
        path = urlparse(full).path
        if not JOBISH.search(path) or path.rstrip("/").count("/") < 1:
            continue
        cands.append((a, full))
    if not cands:
        return []
    counts = {}
    for _, f in cands:
        counts[f] = counts.get(f, 0) + 1
    out, seen = [], set()
    for a, full in cands:
        if full in seen:
            continue
        text = _clean(a.get_text(" "))
        box = a
        for _ in range(5):                                              # grow to the card, but never to a container of many jobs
            parent = box.parent
            if parent is None or parent.name in ("body", "html", "main", "table", "ul", "ol", "section"):
                break
            n = len({urljoin(base_url, x["href"]) for x in parent.find_all("a", href=True) if JOBISH.search(urlparse(urljoin(base_url, x["href"])).path)})
            if n > 1:
                break
            box = parent
        lines = [l.strip() for l in re.split(r"\n+", box.get_text("\n")) if l.strip()]
        lines = [_clean(l) for l in lines]
        title = text if (text and not SKIP_TEXT.match(text) and len(text) > 2) else next(
            (l for l in lines if not SKIP_TEXT.match(l) and len(l) > 3 and not _parse_date(l, False)), "")
        if not title or len(title) > 160:
            continue
        rest = [l for l in lines if l != title and not SKIP_TEXT.match(l) and not _parse_date(l, False)
                and not re.match(r"(full|part)[- ]?time|internship|contract|remote|apply|posted|closing|last date|experience|location", l, re.I)]
        company = next((l for l in rest if 2 < len(l) < 90), "")
        out.append(_mk(park, company, title, full, _parse_date(" ".join(lines))))
        seen.add(full)
    # a real job list repeats: need at least 2 hits, or 1 hit whose path looks like a job detail page
    if len(out) < 2 and not any(re.search(r"/(job|jobs|career|vacanc|opening)[-/s]", j["url"], re.I) for j in out):
        return []
    return out


def jobs_from_html(html, park, base_url, listing_url=None):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    rows = _row_jobs(soup, park, base_url)
    if rows:
        return rows
    return _card_jobs(soup, park, base_url, listing_url or base_url)


def _next_urls(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for a in soup.find_all("a", href=True):
        txt = _clean(a.get_text(" ")).lower()
        href = urljoin(base_url, a["href"])
        rel = " ".join(a.get("rel", [])).lower() if a.get("rel") else ""
        if "next" in rel or txt in ("next", "next »", "»", "›", ">", "next page") or re.search(r"[?&](page|paged|pg|p)=\d+|/page/\d+", href):
            if href.split("#")[0] != base_url and not href.startswith("javascript"):
                urls.append(href.split("#")[0])
    return list(dict.fromkeys(urls))


def _with_page(url, n):
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    q["page"] = str(n)
    return urlunparse(u._replace(query=urlencode(q)))


# ---------- strategy: WordPress REST / API discovery / headless render ----------

def _wp_jobs(client, origin, park, notes):
    jobs = []
    try:
        r = client.get(f"{origin}/wp-json/wp/v2/types")
        if r.status_code != 200:
            notes.append(f"wp-json types: HTTP {r.status_code}")
            return []
        types = r.json()
    except Exception as exc:
        notes.append(f"wp-json: {type(exc).__name__}")
        return []
    for slug, t in (types.items() if isinstance(types, dict) else []):
        blob = f"{slug} {t.get('name', '')} {t.get('rest_base', '')}"
        if not re.search(r"job|career|vacanc|opening|position|recruit", blob, re.I):
            continue
        base = t.get("rest_base") or slug
        for page in range(1, MAX_PAGES() + 1):
            try:
                r = client.get(f"{origin}/wp-json/wp/v2/{base}", params={"per_page": 100, "page": page, "_embed": 1})
                if r.status_code != 200:
                    break
                data = r.json()
            except Exception:
                break
            if not isinstance(data, list) or not data:
                break
            for row in data:
                title = _clean((row.get("title") or {}).get("rendered") if isinstance(row.get("title"), dict) else row.get("title"))
                if not title:
                    continue
                meta = {**(row.get("acf") or {}), **(row.get("meta") or {})} if isinstance(row.get("acf") or row.get("meta"), dict) else {}
                company = _pick(meta, COMPANY_KEYS) if meta else ""
                if not company:
                    for tax in (row.get("_embedded", {}).get("wp:term") or []):
                        for term in tax:
                            if re.search(r"compan|employer|organi", term.get("taxonomy", ""), re.I):
                                company = term.get("name", "")
                desc = ((row.get("content") or {}).get("rendered") if isinstance(row.get("content"), dict) else "") or \
                       ((row.get("excerpt") or {}).get("rendered") if isinstance(row.get("excerpt"), dict) else "")
                jobs.append(_mk(park, company, title, row.get("link") or origin, _ts(row.get("date_gmt") or row.get("date")), strip_html(desc)[:15000]))
            if len(data) < 100:
                break
    notes.append(f"wp-json: {len(jobs)} jobs" if jobs else "wp-json: no job post type")
    return jobs


def _api_candidates(html, page_url, client):
    found = set()
    rx = re.compile(r"""["'`]((?:https?:)?//[^"'`\s]+|/[A-Za-z0-9_\-./]+)(?:\?[^"'`\s]*)?["'`]""")
    texts = [html]
    soup = BeautifulSoup(html, "html.parser")
    origin = "{0.scheme}://{0.netloc}".format(urlparse(page_url))
    for s in soup.find_all("script", src=True)[:8]:
        src = urljoin(page_url, s["src"])
        if urlparse(src).netloc != urlparse(page_url).netloc:
            continue
        try:
            texts.append(client.get(src).text[:600000])
        except Exception:
            pass
    for t in texts:
        for m in rx.finditer(t):
            u = m.group(1)
            if re.search(r"api|ajax|wp-json|\.json|get[-_]?jobs|job[-_]?list|vacanc|careers?/|jobs?/", u, re.I) and \
               not re.search(r"\.(js|css|png|jpe?g|svg|gif|woff2?)(\?|$)", u, re.I):
                found.add(urljoin(origin + "/", u) if not u.startswith("http") else u)
    return sorted(found, key=len)[:10]


def _render(url, park, notes):
    """Headless browser (optional dependency). Collects the page DOM AND every JSON response the page itself loaded."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        notes.append("render: playwright not installed (pip install playwright && playwright install chromium)")
        return []
    jobs, payloads = [], []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(user_agent=BROWSER_HEADERS["User-Agent"],
                                       ignore_https_errors=getattr(settings, "PARK_INSECURE_FALLBACK", True)).new_page()

            def on_resp(resp):
                try:
                    if "json" in (resp.headers.get("content-type") or "") and resp.status == 200:
                        payloads.append((resp.url, resp.json()))
                except Exception:
                    pass
            page.on("response", on_resp)
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
            for n in range(2, MAX_PAGES() + 1):                     # click "next" while it exists
                nxt = page.query_selector("a[rel=next], a:has-text('Next'), button:has-text('Next'), li.next a")
                if not nxt or not nxt.is_enabled():
                    break
                before = len(payloads)
                try:
                    nxt.click(); page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    break
                html += page.content()
            browser.close()
        for u, data in payloads:
            jobs.extend(jobs_from_json(data, park, u))
        jobs = jobs or jobs_from_html(html, park, url) or jobs_from_jsonld(html, park, url)
        notes.append(f"render: {len(jobs)} jobs ({len(payloads)} JSON responses seen)")
    except Exception as exc:
        notes.append(f"render: {type(exc).__name__}: {str(exc)[:120]}")
    return jobs


# ---------- orchestration ----------

def _dedupe(jobs):
    seen, out = set(), []
    for j in jobs:
        k = (j["url"], j["title"].lower())
        if k not in seen and j["title"] and j["url"]:
            seen.add(k)
            out.append(j)
    return out


def read_listing(park, listing_url, client, notes, save_dir=None):
    """Try every strategy on one listing URL. Returns jobs (may be empty); appends human-readable notes."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(listing_url))
    try:
        r = client.get(listing_url)
    except Exception as exc:
        notes.append(f"{listing_url}: {type(exc).__name__}: {str(exc)[:100]}")
        return [], None
    final = str(r.url)
    notes.append(f"{listing_url}: HTTP {r.status_code}, {len(r.content) // 1024} KB" + (f" (redirected to {final})" if final != listing_url else ""))
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"{park}.html").write_bytes(r.content)
    if r.status_code >= 400:
        return [], r
    html, ctype = r.text, r.headers.get("content-type", "")
    if "json" in ctype:
        try:
            jobs = jobs_from_json(r.json(), park, final)
        except ValueError:
            jobs = []
        notes.append(f"json response: {len(jobs)} jobs")
        return _dedupe(jobs), r

    for label, fn in (("json-ld", lambda: jobs_from_jsonld(html, park, final)),
                      ("embedded json", lambda: jobs_from_embedded(html, park, final)),
                      ("html", lambda: jobs_from_html(html, park, final, final))):
        got = fn()
        if got:
            jobs = list(got)
            if label == "html":                                          # follow pagination
                seen_urls, queue, visited = {j["url"] for j in jobs}, _next_urls(html, final), {final}
                pages = 1
                while queue and pages < MAX_PAGES():
                    nxt = queue.pop(0)
                    if nxt in visited:
                        continue
                    visited.add(nxt)
                    try:
                        rr = client.get(nxt)
                        more = jobs_from_html(rr.text, park, str(rr.url), final) if rr.status_code < 400 else []
                    except Exception:
                        break
                    pages += 1
                    fresh = [j for j in more if j["url"] not in seen_urls]
                    if not fresh:
                        continue
                    seen_urls.update(j["url"] for j in fresh)
                    jobs.extend(fresh)
                    queue.extend(u for u in _next_urls(rr.text, str(rr.url)) if u not in visited)
                if pages == 1 and len(jobs) >= 8:                        # no pagination links found: probe ?page=N
                    for n in range(2, MAX_PAGES() + 1):
                        try:
                            rr = client.get(_with_page(final, n))
                            more = jobs_from_html(rr.text, park, str(rr.url), final) if rr.status_code < 400 else []
                        except Exception:
                            break
                        fresh = [j for j in more if j["url"] not in seen_urls]
                        if not fresh:
                            break
                        seen_urls.update(j["url"] for j in fresh)
                        jobs.extend(fresh)
                        pages += 1
                notes.append(f"html: {len(jobs)} jobs over {pages} page(s)")
            else:
                notes.append(f"{label}: {len(jobs)} jobs")
            return _dedupe(jobs), r
    notes.append("html/json-ld/embedded: nothing found" + (" - page looks JavaScript-rendered" if _looks_js_rendered(html) else ""))

    jobs = _wp_jobs(client, origin, park, notes)
    if jobs:
        return _dedupe(jobs), r
    for api in _api_candidates(html, final, client):
        try:
            rr = client.get(api, headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
            if rr.status_code != 200:
                continue
            try:
                got = jobs_from_json(rr.json(), park, api)
            except ValueError:
                got = jobs_from_html(rr.text, park, api, final) if "<" in rr.text[:200] else []
        except Exception:
            continue
        if got:
            notes.append(f"api {api}: {len(got)} jobs")
            return _dedupe(got), r
    notes.append("api discovery: nothing")
    return [], r


def fetch_park_report(key, save_dir=None, force_render=False):
    """-> (jobs, report dict). report = {'park', 'jobs', 'notes': [...], 'ok': bool}"""
    cfg = PARKS[key]
    notes, jobs = [], []
    with SafeClient(settings.JOB_FETCH_TIMEOUT + 6, BROWSER_HEADERS) as client:
        for url in cfg["urls"]:
            if not force_render:
                jobs, _ = read_listing(key, url, client, notes, save_dir)
            if jobs:
                break
            if force_render or getattr(settings, "PARK_RENDER", False):
                jobs = _render(url, key, notes)
                if jobs:
                    break
        if client.insecure_hosts:
            notes.append("TLS certificate of " + ", ".join(sorted(client.insecure_hosts)) + " is invalid (site misconfiguration); "
                         "public listing read without certificate verification")
    return _dedupe(jobs), {"park": key, "name": cfg["name"], "jobs": len(jobs), "notes": notes, "ok": bool(jobs)}


def fetch_park(key):
    """Cached wrapper. Successful lists are cached normally; EMPTY results only briefly (so a fixed site shows up fast)."""
    from . import snapshot
    if snapshot.enabled():
        pj, rep = snapshot.park(key)
        rep = rep or {"park": key, "name": PARKS[key]["name"], "jobs": 0, "ok": False, "notes": ["not in snapshot"]}
        cache.set(f"parkreport:{key}", rep, 86400)
        return pj, rep, True
    ck = f"parkjobs2:{key}"
    hit = cache.get(ck)
    if hit is not None:
        cache.set(f"parkreport:{key}", hit["report"], 86400)
        return hit["jobs"], hit["report"], True
    try:
        jobs, report = fetch_park_report(key)
    except Exception as exc:
        log.warning("park %s failed: %s", key, exc)
        return [], {"park": key, "name": PARKS[key]["name"], "jobs": 0, "ok": False, "notes": [f"{type(exc).__name__}: {str(exc)[:150]}"]}, False
    cache.set(ck, {"jobs": jobs, "report": report}, settings.JOB_CACHE_SECONDS if jobs else getattr(settings, "PARK_EMPTY_CACHE_SECONDS", 300))
    cache.set(f"parkreport:{key}", report, 86400)
    return jobs, report, False


def fetch_selected(keys, progress=None):
    """-> (jobs, errors, n_parks); per-park reports are available via last_reports()."""
    keys = [k for k in dict.fromkeys(keys) if k in PARKS]
    if not keys:
        return [], [], 0
    jobs, errors, reports = [], [], []
    with cf.ThreadPoolExecutor(max_workers=min(4, len(keys))) as pool:
        for i, fut in enumerate([pool.submit(fetch_park, k) for k in keys], 1):
            got, rep, _ = fut.result()
            jobs.extend(got)
            reports.append(rep)
            if not rep["ok"]:
                errors.append(f"{rep['name']} park listing: 0 jobs read - " + " | ".join(rep["notes"][-3:]))
            if progress:
                progress(i, len(keys))
    return jobs, errors, len(keys)


def reports_for(keys):
    """Latest per-park reports (what each strategy found / why it failed) for the results page."""
    return [r for r in (cache.get(f"parkreport:{k}") for k in keys if k in PARKS) if r]
