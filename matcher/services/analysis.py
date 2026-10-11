"""Match a parsed resume against live jobs: relevance, newest-first ordering, gaps and verdicts."""
import hashlib
import re
from datetime import datetime, timezone

from .hubs import HUBS, matching_hubs
from .catalog import functions as job_functions_catalog, industries as industries_catalog, enrich_job
from .jobs import fetch_all, fetch_details, dedupe
from .park_sources import PARKS as PARK_SOURCES, fetch_selected as fetch_park_jobs, reports_for as park_reports_for
from .skills_data import cert_info, find_certs, find_skills, flat_skills, skill_weight

LEVEL_WORDS = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate", "intern", "i", "ii", "iii", "iv",
               "mid", "entry", "level", "trainee", "fresher", "graduate", "aspiring", "the", "and", "of", "for", "a", "an", "in", "at", "-", "&", "/"}
SYNONYMS = {"developer": "engineer", "programmer": "engineer", "swe": "engineer", "sde": "engineer",
            "dev": "engineer", "devops": "devops", "sre": "devops", "frontend": "frontend", "front-end": "frontend",
            "backend": "backend", "back-end": "backend", "fullstack": "fullstack", "full-stack": "fullstack",
            "analytics": "analyst", "ml": "machinelearning", "ai": "machinelearning", "qa": "test", "tester": "test",
            "sdet": "test", "quality": "test"}
PREFERRED_MARK = re.compile(r"nice[\s-]to[\s-]have|preferred|bonus|good[\s-]to[\s-]have|a plus|\bplus\b|desirable", re.I)
_N = r"(?<![\d.])(?P<n>\d{1,2}(?:\.\d{1,2})?)"            # 1, 12, 1.5  (never the "5" inside "1.5")
_U = r"(?P<u>years?|yrs?|months?|mos?)\b"
YEARS_REQ = [
    re.compile(_N + r"\s*\+?\s*(?:(?:-|to|\u2013)\s*\d{1,2}(?:\.\d{1,2})?\s*\+?\s*)?" + _U + r"[^.\n]{0,70}?experience", re.I),
    re.compile(r"experience[^.\n]{0,50}?" + _N + r"\s*\+?\s*" + _U, re.I),
    re.compile(r"minimum (?:of )?" + _N + r"\s*\+?\s*" + _U, re.I),
]


# Role families: two titles in the same family are related even when no word is shared (UI/UX Designer ~ Graphic Designer).
FAMILIES = {
    "design": {"ui", "ux", "uiux", "design", "designer", "graphic", "visual", "interaction", "creative", "figma", "illustrator", "animator", "motion"},
    "software": {"engineer", "frontend", "backend", "fullstack", "software", "web", "mobile", "android", "ios", "programmer", "react", "python", "java", "node"},
    "data": {"data", "analyst", "analytics", "scientist", "machinelearning", "bi", "statistician"},
    "test": {"test", "qa", "quality", "automation"},
    "devops": {"devops", "cloud", "infrastructure", "sysadmin", "network", "security"},
    "product": {"product", "manager", "owner", "scrum", "project", "program"},
    "marketing": {"marketing", "seo", "content", "social", "brand", "growth", "copywriter"},
}
FAMILY_WEIGHT = 0.4        # credit for "same family, different words"
DESIGN_ALIASES = {"designer": "design", "designers": "design", "designing": "design", "uiux": "uiux"}


def _tokens(title):
    t = (title or "").lower()
    t = re.sub(r"\b(ui|ux)\s*[-/&+and]*\s*(ui|ux)\b", " ui ux ", t)               # UI/UX, UX-UI, UI & UX
    t = re.sub(r"(entry|mid)[- ]?level", " ", t)
    t = re.sub(r"\b(front|back|full)[- ]?(end|stack)\b", lambda m: m.group(1) + m.group(2), t)
    toks = re.findall(r"[a-z][a-z+#.]*", t)
    toks = [DESIGN_ALIASES.get(SYNONYMS.get(x, x), SYNONYMS.get(x, x)) for x in toks]
    return {x for x in toks if x not in LEVEL_WORDS and len(x) > 1}


def _family_tags(tokens):
    return {f for f, words in FAMILIES.items() if tokens & words}


# Words that name a kind of job but not a field: "analyst" alone says nothing about WHICH analyst.
GENERIC_ROLE = {"analyst", "engineer", "manager", "specialist", "executive", "officer", "consultant", "scientist", "representative",
                "coordinator", "administrator", "assistant", "architect", "associate", "developer", "lead"}


def _title_best(weighted_titles, job_title):
    """weighted_titles = [(title, weight)]. Returns (score 0..1, domain) where domain means the job shares a real field word
    (data, python, frontend ...) with the candidate - not only a generic noun such as 'analyst' or 'engineer'."""
    jt = _tokens(job_title)
    if not jt or not weighted_titles:
        return 0.0, False
    jf = _family_tags(jt)
    best, domain = 0.0, False
    for ct, weight in weighted_titles:
        c = _tokens(ct)
        if not c:
            continue
        overlap = c & jt
        score = len(overlap) / len(jt | c) * 0.5 + len(overlap) / len(c) * 0.5
        shared = _family_tags(c) & jf
        if shared and not overlap or (shared and score < FAMILY_WEIGHT):
            score = max(score, FAMILY_WEIGHT)
        score *= weight
        real_overlap = bool(overlap - GENERIC_ROLE)
        real_family = any((c & FAMILIES[f]) - GENERIC_ROLE and (jt & FAMILIES[f]) - GENERIC_ROLE for f in shared)
        if score > best:
            best, domain = score, real_overlap or real_family
        elif score == best and (real_overlap or real_family):
            domain = True
    return round(min(1.0, best * 1.15), 3), domain


def title_score(candidate_titles, job_title):
    """Plain title similarity, every candidate title counting the same."""
    return _title_best([(t, 1.0) for t in candidate_titles or []], job_title)[0]


def title_match(parsed, job_title):
    """Resume-aware title similarity. The role the person is aiming for (summary/headline) and their latest role count in full;
    older or short stints (a one-month internship in another field) count 60%, so they cannot pull in unrelated jobs."""
    main = {parsed.get("primary_title"), parsed.get("latest_title")}
    return _title_best([(t, 1.0 if t in main else 0.6) for t in parsed.get("titles", [])], job_title)


FRESHER_TITLE = re.compile(
    r"\b(fresher|freshers|fresh graduates?|entry[- ]level|graduates?|trainee|trainees|interns?|internship|apprentice(?:ship)?|campus hire)\b", re.I)
FRESHER_TEXT = re.compile(
    r"\bfreshers?\s+(?:are\s+|can\s+|may\s+)?(?:welcome|apply|encouraged|eligible)"
    r"|\b(?:no|zero)\s+(?:prior\s+|previous\s+|work\s+)?experience\s+(?:is\s+)?(?:required|needed|necessary)"
    r"|\bentry[- ]level\b|\b0\s*(?:-|to|–)\s*1\s*years?\b", re.I)


def is_fresher_friendly(title, description):
    """True when the job is meant for people with no experience: a fresher/entry-level/trainee/intern title,
    or the text says freshers are welcome / no experience is required."""
    return bool(FRESHER_TITLE.search(title or "") or FRESHER_TEXT.search(description or ""))


def required_years(text):
    """Years of experience a posting asks for, as a number of years (0 = it states nothing).

    Understands "3 years", "1.5 years", "7 months" (= 0.58) and ranges like "1-2 years" (the lower number counts).
    Whole numbers come back as int, others rounded to 2 decimals.
    """
    for rx in YEARS_REQ:
        for m in rx.finditer(text):
            v = float(m.group("n"))
            if m.group("u").lower().startswith("m"):
                v = v / 12
            v = round(v, 2)
            if 0 < v <= 25:
                return int(v) if v == int(v) else v
    return 0


# ---- reading a job posting ----
# Lines about the company (not the role) mention AI, data, "5 years of..." etc. and used to create fake skill/experience matches.
_KEEP_HEAD = re.compile(
    r"responsibilit|what you.{0,25}(do|bring|need|have|will)|requirement|qualification|skills|looking for|you have|you.ll bring|must[- ]have|"
    r"nice[- ]to[- ]have|the role|your (impact|role|day)|in this role|day[- ]to[- ]day|minimum|preferred|experience|who you are|about you|"
    r"the job|the position|the internship|key (duties|tasks)|job description|duties", re.I)
_COMPANY_START = ("who we are", "our mission", "our story", "our values", "our culture", "our vision", "our company", "our team",
                  "company overview", "company description", "company culture", "company profile", "why join", "why work",
                  "benefits", "perks", "what we offer", "what's in it for you", "compensation", "salary", "pay range",
                  "equal opportunity", "eeo", "diversity", "how to apply", "application process", "privacy", "life at")
_ABOUT_KEEP = ("the role", "the job", "the position", "this role", "this job", "this position", "you", "yourself", "the opportunity",
               "the internship", "the team")


def _is_company_heading(t):
    h = t.strip(" :*#-").lower()
    if h.startswith("about "):
        return not h[6:].startswith(_ABOUT_KEEP)
    return h.startswith(_COMPANY_START)


def job_body(description):
    """The part of a posting that describes the ROLE: company / benefits / legal sections are dropped."""
    description = description or ""
    out, skip = [], False
    for line in description.splitlines():
        t = line.strip()
        short = bool(t) and len(t.split()) <= 9 and not t.endswith((".", ",", ";"))
        if short and _is_company_heading(t):
            skip = True
            continue
        if short and skip and (_KEEP_HEAD.search(t) or (t.isupper() and len(t) > 3)):
            skip = False
        if not skip:
            out.append(line)
    body = "\n".join(out)
    return body if len(body) >= 120 and len(body) >= 0.1 * len(description) else description     # nearly nothing left = misread, use all


# Seniority words in the title imply experience even when the text never says "5 years".
LEVEL_YEARS = [(re.compile(r"\b(vp|vice president|chief)\b", re.I), 12), (re.compile(r"\b(director|head of)\b", re.I), 10),
               (re.compile(r"\bprincipal\b", re.I), 8), (re.compile(r"\bstaff\b", re.I), 7),
               (re.compile(r"\b(lead|manager)\b", re.I), 5), (re.compile(r"\b(senior|sr)\b\.?", re.I), 4)]


def implied_years(title):
    for rx, yrs in LEVEL_YEARS:
        if rx.search(title or ""):
            return yrs
    return 0


def compute_requirements(job):
    """What a posting asks for: skills (required / preferred), certificates, years of experience, fresher-friendly."""
    title, description = job.get("title", ""), job.get("description", "") or ""
    body = job_body(description)
    cut = PREFERRED_MARK.search(body)
    required_text = f"{title}\n{body[:cut.start()]}" if cut and cut.start() > 60 else f"{title}\n{body}"
    preferred_text = body[cut.start():] if cut and cut.start() > 60 else ""
    req = flat_skills(find_skills(required_text))
    pref = [s for s in flat_skills(find_skills(preferred_text)) if s not in req]
    fresher = is_fresher_friendly(title, description)
    years, src = required_years(body), ""
    if years:
        src = "stated"
    elif not fresher and implied_years(title):
        years, src = implied_years(title), "title"
    if fresher and src != "stated":
        years, src = 0, ""
    return {"required": req, "preferred": pref, "certs": find_certs(f"{title}\n{body}"), "years": years,
            "years_src": src, "fresher": fresher}


def job_requirements(job):
    return job.get("req") or compute_requirements(job)          # the snapshot already carries it (computed on GitHub)


def _wsum(skills):
    return sum(skill_weight(s) for s in skills)


def evaluate(parsed, job):
    reqs = job_requirements(job)
    have = set(parsed["skills_flat"])
    have_certs = set(parsed.get("certs_detected", []))
    cert_text = " ".join(parsed.get("certifications", [])).lower()

    matched_req = [s for s in reqs["required"] if s in have]
    matched_pref = [s for s in reqs["preferred"] if s in have]
    missing_req = [s for s in reqs["required"] if s not in have]
    missing_pref = [s for s in reqs["preferred"] if s not in have]

    # Only technical / domain skills decide the score. "Communication" and "Problem Solving" appear in every posting and every
    # resume, so they are shown but weigh 0; generic practices (Git, Agile ...) weigh half.
    weight_total = _wsum(reqs["required"]) + 0.5 * _wsum(reqs["preferred"])
    skill_known = weight_total > 0
    skill_part = (_wsum(matched_req) + 0.5 * _wsum(matched_pref)) / weight_total if skill_known else 0.35
    core_matched = [s for s in matched_req + matched_pref if skill_weight(s) >= 1]
    missing_core = [s for s in missing_req if skill_weight(s) > 0]

    t_score, domain = title_match(parsed, job["title"])
    yrs_need, yrs_have = reqs["years"], parsed.get("years_experience", 0)
    fresher = bool(reqs.get("fresher"))
    exp_fit = fresher or not yrs_need or yrs_need <= yrs_have + 0.05       # fresher / no experience asked: open to everyone
    exp_part = 1.0 if exp_fit else min(1.0, yrs_have / yrs_need)

    score = round(100 * (0.55 * skill_part + 0.30 * t_score + 0.15 * exp_part))

    missing_certs = []
    for c in reqs["certs"]:
        generic = c.split(" Certification")[0].lower()
        if c in have_certs or (generic and generic in cert_text and c.endswith("Certification")):
            continue
        missing_certs.append(cert_info(c))

    if score >= 70 and not missing_certs and len(missing_core) <= 1 and core_matched and exp_fit:
        verdict, label = "eligible", "Strong match"
    elif score >= 70 and missing_certs and core_matched and exp_fit:
        verdict, label = "certs", "Strong match, certificate needed"
    elif score >= 45 and core_matched and not exp_fit and yrs_need - yrs_have >= 1:
        verdict, label = "partial", "Good fit, needs more experience"
    elif score >= 45:
        verdict, label = "partial", "Close, resume needs work"
    else:
        verdict, label = "ineligible", "Not eligible yet"

    return {
        "key": hashlib.sha1(job["url"].encode()).hexdigest()[:16],
        **{k: job[k] for k in ("title", "company", "location", "url", "posted")},
        "score": score,
        "verdict": verdict,
        "verdict_label": label,
        "title_score": round(t_score * 100),
        "domain_match": domain,
        "skill_score": round(skill_part * 100),
        "skill_known": skill_known,
        "core_matched": len(core_matched),
        "matched": matched_req + matched_pref,
        "missing_required": missing_req,
        "missing_preferred": missing_pref,
        "years_needed": yrs_need,
        "years_src": reqs.get("years_src", ""),
        "fresher": fresher,
        "exp_fit": exp_fit,
        "years_have": yrs_have,
        "years_gap": max(0, round(yrs_need - yrs_have, 1)) if yrs_need and not fresher else 0,
        "missing_certs": missing_certs,
        "needs_resume_update": bool(missing_req or missing_pref) and verdict != "eligible",
        "snippet": re.sub(r"\s+", " ", job.get("description", ""))[:260],
    }


def is_relevant(ev):
    """Why a posting is worth showing: its title fits the candidate's field AND real technical skills back it up,
    or its technical skills clearly fit on their own. One stray keyword ("Python" in a security-analyst post) is not enough."""
    core = ev["core_matched"]
    skills_ok = ev["skill_score"] >= 20 or not ev["skill_known"]
    if not ev["skill_known"] and ev["title_score"] >= 50 and ev["domain_match"]:
        return True                                          # no description to read yet, but the title is clearly the candidate's field
    if ev["title_score"] >= 35 and core >= 1 and skills_ok and (ev["domain_match"] or core >= 2):
        return True
    if core >= 2 and ev["skill_score"] >= 40 and ev["title_score"] >= 15:
        return True
    return core >= 3 and ev["skill_score"] >= 50            # found through skills alone (different job title)


OVER_EXPERIENCE_SHOWN = 15          # jobs asking for more years than the resume has are kept, at the bottom


def order_results(results, limit):
    """Jobs the candidate's experience fits come first (newest, then best match); jobs that ask for more years than the resume
    shows - for example a '5+ years' role for a fresher - are not hidden but listed last."""
    key = lambda r: (_posted_key(r), r["score"])
    fit = sorted((r for r in results if r["exp_fit"]), key=key, reverse=True)
    over = sorted((r for r in results if not r["exp_fit"]), key=key, reverse=True)
    return fit[:limit] + over[:OVER_EXPERIENCE_SHOWN]


def _posted_key(r):
    try:
        return datetime.fromisoformat(r["posted"])
    except (ValueError, TypeError, KeyError):
        return datetime.min.replace(tzinfo=timezone.utc)


def search_and_rank(parsed, limit=40, companies=None, progress=None, hubs=None, job_functions=None, industries=None):
    """Return (results, stats). Results are relevant jobs, newest posted first.

    hubs: optional list of keys from hubs.HUBS. When given, (a) company-career-page jobs are kept only if their
    location belongs to a selected hub, and (b) the configured job-board APIs (Adzuna / Jooble / JSearch) are queried
    for each job title in each hub. Without hubs the behaviour is exactly the old company-pages-only search.
    industries: optional industry keys; a job is retained when its classified posting industry OR registered
    company industry matches one of the selected keys.

    Speed: titles are screened first (cheap); full descriptions are downloaded only for the newest
    title-relevant postings, then those are scored in detail.
    """
    from django.conf import settings

    hubs = [h for h in (hubs or []) if h in HUBS]
    available_functions = set(job_functions_catalog())
    job_functions = [f for f in (job_functions or []) if f in available_functions]
    available_industries = set(industries_catalog())
    industries = [i for i in (industries or []) if i in available_industries]
    titles = parsed.get("titles", [])

    jobs, errors, n_companies = fetch_all(companies, progress=progress)
    n_ats_jobs = len(jobs)

    park_calls = 0
    n_park_jobs = 0
    if hubs:
        # A park selection is a first-class source now. Official park listings are merged
        # with employer ATS listings; location matching remains a fallback for ATS jobs.
        jobs = [j for j in jobs if matching_hubs(j["location"], hubs)]
        offset = n_companies
        park_jobs, park_errors, park_calls = fetch_park_jobs(
            hubs, progress=(lambda d, t: progress(offset + d, offset + t)) if progress else None)
        jobs.extend(park_jobs)
        n_park_jobs = len(park_jobs)
        errors.extend(park_errors)
        jobs = dedupe(jobs)
        # Do not discard official park jobs just because their detail page omits the park name.
        official_urls = {j["url"] for j in park_jobs}
        jobs = [j for j in jobs if j["url"] in official_urls or matching_hubs(j["location"], hubs)]
    else:
        # Default for EVERY search: the official park listings (Infopark, Technopark, Cyberpark) are merged in with the
        # company career pages - no selection needed and ATS jobs are not restricted by location.
        park_jobs, park_errors, park_calls = fetch_park_jobs(list(PARK_SOURCES))
        n_park_jobs = len(park_jobs)
        errors.extend(park_errors)
        jobs = dedupe(jobs + park_jobs)

    if job_functions:
        jobs = [j for j in jobs if set(enrich_job(j).get("job_functions", [])) & set(job_functions)]
    if industries:
        # Keep jobs whose classified posting industry OR registered company industry matches.
        filtered = []
        for job in jobs:
            enriched = enrich_job(job)
            if (set(enriched.get("job_industries", [])) | set(enriched.get("company_industries", []))) & set(industries):
                filtered.append(job)
        jobs = filtered

    have = set(parsed.get("skills_flat", []))
    candidates, seen = [], set()
    for j in jobs:
        if title_match(parsed, j["title"])[0] >= 0.15:
            candidates.append(j)
            seen.add(j["url"])
    candidates.sort(key=_posted_key, reverse=True)
    candidates = candidates[: settings.JOB_DETAIL_LIMIT]
    fetch_details(candidates)
    seen = {j["url"] for j in candidates}
    for j in jobs:                           # skill-based discovery: different title, but the technical skills clearly match
        if j["url"] in seen or not j.get("req"):
            continue
        req = j["req"]
        if sum(1 for s in req["required"] + req["preferred"] if skill_weight(s) >= 1 and s in have) >= 3:
            candidates.append(j)

    results = []
    for job in candidates:
        job = enrich_job(job)
        ev = evaluate(parsed, job)
        if is_relevant(ev):
            ev["hubs"] = [HUBS[k]["label"] for k in matching_hubs(job["location"], hubs)] if hubs else []
            ev["sources"] = job.get("sources", [])
            ev["job_functions"] = job.get("job_function_labels", [])
            ev["job_industries"] = job.get("job_industry_labels", [])
            ev["company_industries"] = job.get("company_industries", [])
            results.append(ev)

    n_matched = len(results)
    results = order_results(results, limit)
    return results, {
        "companies": n_companies, "jobs_scanned": len(jobs), "unreachable": errors,
        "searched_titles": titles, "matched": n_matched, "over_experience": sum(1 for r in results if not r["exp_fit"]),
        "hubs": [HUBS[k]["label"] for k in hubs], "board_providers": [],
        "park_sources": [HUBS[k].get("source") for k in hubs if HUBS[k].get("source")],
        "park_calls": park_calls, "ats_jobs_before_hub_filter": n_ats_jobs,
        "park_reports": park_reports_for(hubs or list(PARK_SOURCES)), "park_jobs": n_park_jobs,
        "job_functions": job_functions, "industries": industries,
    }


# ---------------------------------------------------------------- search by skills

def parse_skill_query(query):
    """'python, SQL and power bi' -> (['Python', 'SQL', 'Power BI'], []). Words that are not in the skill dictionary
    (a tool, a domain word) are kept as plain keywords and searched in the posting text."""
    skills, keywords = [], []
    for part in re.split(r"[,;\n|]+|\s+and\s+|\s+&\s+", query or "", flags=re.I):
        part = part.strip(" .")
        if len(part) < 2:
            continue
        found = flat_skills(find_skills(part))
        if found:
            skills.extend(s for s in found if s not in skills)
        elif part.lower() not in keywords:
            keywords.append(part.lower())
    return skills[:12], keywords[:8]


def search_by_skills(parsed, query, limit=60):
    """Jobs that ask for the typed skills - whatever their title is. Ranked by how many of the typed skills they need.
    Experience works as everywhere else: jobs that fit the resume first, jobs asking for more years last."""
    from .jobs import fetch_all as _fetch_all
    skills, keywords = parse_skill_query(query)
    total = len(skills) + len(keywords)
    stats = {"skills": skills, "keywords": keywords, "matched": 0, "jobs_scanned": 0, "over_experience": 0}
    if not total:
        return [], stats

    jobs, _errors, _n = _fetch_all(None)
    park_jobs, _pe, _pc = fetch_park_jobs(list(PARK_SOURCES))
    jobs = dedupe(jobs + park_jobs)
    stats["jobs_scanned"] = len(jobs)
    wanted_lc = [s.lower() for s in skills]

    results = []
    for job in jobs:
        req = job.get("req") or (compute_requirements(job) if job.get("description") else None)
        title_lc = job["title"].lower()
        if req:
            job_skills = set(req["required"]) | set(req["preferred"])
            hit_skills = [s for s in skills if s in job_skills or s.lower() in title_lc]
        else:                                                    # title only (live mode, description not downloaded)
            hit_skills = [s for s, lc in zip(skills, wanted_lc) if lc in title_lc]
        text_lc = None
        hit_keywords = []
        for k in keywords:
            if text_lc is None:
                text_lc = f"{title_lc}\n{(job.get('description') or '').lower()}"
            if k in text_lc:
                hit_keywords.append(k)
        hits = hit_skills + hit_keywords
        if not hits:
            continue
        ev = evaluate(parsed, enrich_job(job))
        ev["query_matched"] = hits
        ev["query_total"] = total
        ev["sources"] = job.get("sources", [])
        results.append(ev)

    stats["matched"] = len(results)
    results.sort(key=_posted_key, reverse=True)                  # newest first ...
    results.sort(key=lambda r: r["score"], reverse=True)         # ... then best match ...
    results.sort(key=lambda r: len(r["query_matched"]), reverse=True)   # ... but most typed skills first
    fit = [r for r in results if r["exp_fit"]]
    over = [r for r in results if not r["exp_fit"]]
    out = fit[:limit] + over[:OVER_EXPERIENCE_SHOWN]
    stats["over_experience"] = len(over[:OVER_EXPERIENCE_SHOWN])
    return out, stats
