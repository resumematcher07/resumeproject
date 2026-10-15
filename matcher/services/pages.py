"""Readers for career pages that are NOT a public ATS feed.

    ats: html       -> a job-search page whose HTML already contains the job links
                       (Bank of America, BlackRock / any Radancy "TalentBrew" site, many corporate portals)
    ats: eightfold  -> an Eightfold.ai career portal (HSBC and many banks); the page itself is a JavaScript app, so we
                       read the JSON the app calls instead of the HTML

Registry examples (companies.yaml):

  - {name: Bank of America, ats: html, url: "https://careers.bankofamerica.com/en-us/job-search/india",
     link_pattern: "/job-detail/", page_param: start, page_step: 10, max_pages: 10}
  - {name: BlackRock, ats: html, url: "https://careers.blackrock.com/search-jobs",
     link_pattern: "/job/", page_param: p, page_start: 1, page_step: 1, max_pages: 10}
  - {name: HSBC, ats: eightfold, url: "https://portal.careers.hsbc.com/careers", domain: hsbc.com, location: India}

Descriptions are NOT on list pages, so each job carries detail={"url": ...}; the existing detail fetcher downloads the job
page later, only for jobs that matter (see jobs.fetch_detail).
"""
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse

from bs4 import BeautifulSoup

GENERIC = {"apply", "apply now", "view job", "view details", "read more", "learn more", "details", "view"}
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")


def _jobs_mod():
    from . import jobs            # late import: jobs.py imports this module
    return jobs


def _set_param(url, key, value):
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != key]
    q.append((key, str(value)))
    return urlunparse(p._replace(query=urlencode(q)))


def _parse_date(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        if m.group(1):                                  # US style mm/dd/yyyy
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
        return datetime(int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=timezone.utc)
    except ValueError:
        return None


def _card(anchor, link_pattern):
    """The smallest ancestor of this link that holds only ONE job link: the job's 'card'."""
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None or parent.name in ("body", "html"):
            break
        if len({x["href"] for x in parent.find_all("a", href=True)
                if link_pattern in x["href"] and x.get_text(" ", strip=True).casefold() not in GENERIC}) > 1:
            break
        node = parent
    return node


def _lines(node):
    out = []
    for s in node.get_text("\n").splitlines():
        s = re.sub(r"\s+", " ", s).strip()
        if s and s not in out:
            out.append(s)
    return out


def parse_listing(html_text, page_url, link_pattern="/job"):
    """Job cards found in one HTML result page -> list of dicts(title, url, location, posted)."""
    soup = BeautifulSoup(html_text, "html.parser")
    seen, jobs = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if link_pattern not in href or href.startswith(("mailto:", "javascript:")):
            continue
        if a.get_text(" ", strip=True).casefold() in GENERIC:        # "Apply" buttons duplicate the title link
            continue
        url = urljoin(page_url, href).split("#")[0]
        if url in seen:
            continue
        # one job can have several links (title + "Apply"): use the card, take the title from the heading if any
        card = _card(a, link_pattern)
        head = card.find(["h1", "h2", "h3", "h4"]) or a
        lines = _lines(head)
        title = lines[0] if lines else ""
        if not title or len(title) > 160:
            title = (_lines(a) or [""])[0]
        if not title or len(title) > 160:
            continue
        seen.add(url)
        loc = ""
        el = card.find(class_=re.compile(r"location|city|place", re.I))
        if el:
            loc = (_lines(el) or [""])[0]
        date_el = card.find(class_=re.compile(r"date|posted", re.I))
        posted = _parse_date(date_el.get_text(" ") if date_el else "") or _parse_date(" ".join(_lines(card)))
        if not loc:                                      # banks list "...unit, area, DATE, PLACE": the line after the date
            ls = _lines(card)
            idx = next((i for i, l in enumerate(ls) if DATE_RE.search(l)), None)
            if idx is not None and idx + 1 < len(ls) and len(ls[idx + 1]) < 80:
                loc = ls[idx + 1]
        if not loc:                                      # fallback: first short line that is not the title / a date / a label
            for l in _lines(card):
                if l != title and not DATE_RE.search(l) and len(l) < 80 and not re.match(r"(posted|apply|new|req)", l, re.I):
                    loc = l
                    break
        jobs.append({"title": title, "url": url, "location": loc, "posted": posted})
    return jobs


def _html(c, client):
    jm = _jobs_mod()
    base = c["url"]
    pat = c.get("link_pattern", "/job")
    param = c.get("page_param")
    step = int(c.get("page_step", 1))
    first = int(c.get("page_start", 0))
    pages = int(c.get("max_pages", 1 if not param else 5))
    seen = set()
    for i in range(pages):
        url = base if (i == 0 and not param) else (_set_param(base, param, first + i * step) if param else base)
        r = client.get(url)
        r.raise_for_status()
        new = [j for j in parse_listing(r.text, str(r.url), pat) if j["url"] not in seen]
        if not new:
            break                                        # past the last page / pagination not honoured
        for j in new:
            seen.add(j["url"])
            yield jm._job(c["name"], j["title"], j["url"], j["location"], j["posted"], "", detail={"url": j["url"]})


def _eightfold(c, client):
    """Eightfold portals expose their search as JSON (the same call the web page makes)."""
    jm = _jobs_mod()
    p = urlparse(c["url"])
    origin = f"{p.scheme}://{p.netloc}"
    domain = c.get("domain") or p.netloc.replace("portal.careers.", "").replace("careers.", "")
    pages = int(c.get("max_pages", 10))
    num = int(c.get("page_size", 10))
    seen = set()
    for i in range(pages):
        params = {"domain": domain, "start": i * num, "num": num, "query": c.get("query", ""), "location": c.get("location", "")}
        data = None
        for path in ("/api/apply/v2/jobs", "/api/pcsx/search"):
            r = client.get(origin + path, params=params)
            if r.status_code == 200:
                try:
                    data = r.json()
                    break
                except ValueError:
                    continue
        if data is None:
            if i == 0:
                raise RuntimeError("Eightfold JSON endpoint not reachable")
            break
        positions = data.get("positions") or (data.get("data") or {}).get("positions") or []
        positions = [x for x in positions if x.get("id") not in seen]
        if not positions:
            break
        for x in positions:
            seen.add(x.get("id"))
            loc = x.get("location") or ", ".join(x.get("locations") or [])
            url = x.get("canonicalPositionUrl") or x.get("canonical_position_url") or f"{origin}/careers/job/{x.get('id')}"
            posted = jm._ts(x.get("t_create") or x.get("postedTs"))
            yield jm._job(c["name"], x.get("name") or x.get("title"), url, loc, posted,
                          jm.strip_html(x.get("job_description") or ""),
                          detail={"ats": "eightfold", "token": origin, "id": x.get("id"), "domain": domain})


def eightfold_detail(client, origin, pid, domain):
    r = client.get(f"{origin}/api/apply/v2/jobs/{pid}", params={"domain": domain})
    r.raise_for_status()
    return _jobs_mod().strip_html(r.json().get("job_description"))[:15000]


def ldjson_description(html_text):
    """Description from schema.org JobPosting data in a job page, '' when absent."""
    import json
    jm = _jobs_mod()
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for p in jm._walk_jobposting(data):
            text = jm.strip_html(p.get("description"))
            if len(text) > 200:
                return text[:15000]
    return ""
