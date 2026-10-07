"""Match a parsed resume against live jobs: relevance, newest-first ordering, gaps and verdicts."""
import hashlib
import re
from datetime import datetime, timezone

from .hubs import HUBS, matching_hubs
from .catalog import functions as job_functions_catalog, industries as industries_catalog, enrich_job
from .jobs import fetch_all, fetch_details, dedupe
from .park_sources import PARKS as PARK_SOURCES, fetch_selected as fetch_park_jobs, reports_for as park_reports_for
from .skills_data import cert_info, find_certs, find_skills, flat_skills

LEVEL_WORDS = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate", "intern", "i", "ii", "iii", "iv",
               "mid", "entry", "level", "trainee", "fresher", "graduate", "aspiring", "the", "and", "of", "for", "a", "an", "in", "at", "-", "&", "/"}
SYNONYMS = {"developer": "engineer", "programmer": "engineer", "swe": "engineer", "sde": "engineer",
            "dev": "engineer", "devops": "devops", "sre": "devops", "frontend": "frontend", "front-end": "frontend",
            "backend": "backend", "back-end": "backend", "fullstack": "fullstack", "full-stack": "fullstack",
            "analytics": "analyst", "ml": "machinelearning", "ai": "machinelearning", "qa": "test", "tester": "test",
            "sdet": "test", "quality": "test"}
PREFERRED_MARK = re.compile(r"nice[\s-]to[\s-]have|preferred|bonus|good[\s-]to[\s-]have|a plus|\bplus\b|desirable", re.I)
YEARS_REQ = [
    re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)[^.\n]{0,70}?experience", re.I),
    re.compile(r"experience[^.\n]{0,50}?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
    re.compile(r"minimum (?:of )?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I),
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


def title_score(candidate_titles, job_title):
    jt = _tokens(job_title)
    if not jt or not candidate_titles:
        return 0.0
    jf = _family_tags(jt)
    best = 0.0
    for ct in candidate_titles:
        c = _tokens(ct)
        if not c:
            continue
        overlap = len(c & jt)
        score = overlap / len(jt | c) * 0.5 + overlap / len(c) * 0.5
        shared = _family_tags(c) & jf
        if shared and not overlap or (shared and score < FAMILY_WEIGHT):
            score = max(score, FAMILY_WEIGHT)
        best = max(best, score)
    return round(min(1.0, best * 1.15), 3)


def required_years(text):
    for rx in YEARS_REQ:
        m = rx.search(text)
        if m and 0 < int(m.group(1)) <= 25:
            return int(m.group(1))
    return 0


def job_requirements(job):
    text = f"{job['title']}\n{job['description']}"
    cut = PREFERRED_MARK.search(job["description"])
    required_text = f"{job['title']}\n{job['description'][:cut.start()]}" if cut and cut.start() > 60 else text
    preferred_text = job["description"][cut.start():] if cut and cut.start() > 60 else ""
    req = flat_skills(find_skills(required_text))
    pref = [s for s in flat_skills(find_skills(preferred_text)) if s not in req]
    return {"required": req, "preferred": pref, "certs": find_certs(text), "years": required_years(job["description"])}


def evaluate(parsed, job):
    reqs = job_requirements(job)
    have = set(parsed["skills_flat"])
    have_certs = set(parsed.get("certs_detected", []))
    cert_text = " ".join(parsed.get("certifications", [])).lower()

    matched_req = [s for s in reqs["required"] if s in have]
    matched_pref = [s for s in reqs["preferred"] if s in have]
    missing_req = [s for s in reqs["required"] if s not in have]
    missing_pref = [s for s in reqs["preferred"] if s not in have]

    weight_total = len(reqs["required"]) + 0.5 * len(reqs["preferred"])
    skill_part = (len(matched_req) + 0.5 * len(matched_pref)) / weight_total if weight_total else 0.5

    t_score = title_score(parsed.get("titles", []), job["title"])
    yrs_need, yrs_have = reqs["years"], parsed.get("years_experience", 0)
    exp_part = 1.0 if not yrs_need else min(1.0, yrs_have / yrs_need)

    score = round(100 * (0.55 * skill_part + 0.30 * t_score + 0.15 * exp_part))

    missing_certs = []
    for c in reqs["certs"]:
        generic = c.split(" Certification")[0].lower()
        if c in have_certs or (generic and generic in cert_text and c.endswith("Certification")):
            continue
        missing_certs.append(cert_info(c))

    if score >= 70 and not missing_certs and len(missing_req) <= 1:
        verdict, label = "eligible", "Strong match"
    elif score >= 70 and missing_certs:
        verdict, label = "certs", "Strong match, certificate needed"
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
        "skill_score": round(skill_part * 100),
        "matched": matched_req + matched_pref,
        "missing_required": missing_req,
        "missing_preferred": missing_pref,
        "years_needed": yrs_need,
        "years_have": yrs_have,
        "years_gap": max(0, round(yrs_need - yrs_have, 1)) if yrs_need else 0,
        "missing_certs": missing_certs,
        "needs_resume_update": bool(missing_req or missing_pref) and verdict != "eligible",
        "snippet": re.sub(r"\s+", " ", job["description"])[:260],
    }


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

    candidates = [j for j in jobs if title_score(titles, j["title"]) >= 0.15]
    candidates.sort(key=_posted_key, reverse=True)
    candidates = candidates[: settings.JOB_DETAIL_LIMIT]
    fetch_details(candidates)

    results = []
    for job in candidates:
        job = enrich_job(job)
        ev = evaluate(parsed, job)
        if ev["title_score"] >= 35 or (ev["title_score"] >= 15 and ev["skill_score"] >= 40):
            ev["hubs"] = [HUBS[k]["label"] for k in matching_hubs(job["location"], hubs)] if hubs else []
            ev["sources"] = job.get("sources", [])
            ev["job_functions"] = job.get("job_function_labels", [])
            ev["job_industries"] = job.get("job_industry_labels", [])
            ev["company_industries"] = job.get("company_industries", [])
            results.append(ev)

    results.sort(key=lambda r: (_posted_key(r), r["score"]), reverse=True)     # newest first, then best match
    return results[:limit], {
        "companies": n_companies, "jobs_scanned": len(jobs), "unreachable": errors,
        "searched_titles": titles, "matched": len(results),
        "hubs": [HUBS[k]["label"] for k in hubs], "board_providers": [],
        "park_sources": [HUBS[k].get("source") for k in hubs if HUBS[k].get("source")],
        "park_calls": park_calls, "ats_jobs_before_hub_filter": n_ats_jobs,
        "park_reports": park_reports_for(hubs or list(PARK_SOURCES)), "park_jobs": n_park_jobs,
        "job_functions": job_functions, "industries": industries,
    }
