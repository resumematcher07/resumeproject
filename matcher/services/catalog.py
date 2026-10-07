"""Load global company, job-function, industry and IT-park metadata.

The registry is deliberately file-backed so adding companies does not require a database migration.
"""
from pathlib import Path
import re

import yaml
from django.conf import settings


# CSafeLoader is ~10x faster than the pure-Python loader when libyaml is available.
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_FILE_CACHE = {}      # filename -> (path, mtime_ns, data); reloads automatically when the file is edited
_INDEX_CACHE = {}     # id(companies list) -> {name: company}


def _load(filename, default):
    """Load a YAML registry file ONCE and re-read it only if it changes on disk.

    Previously every call re-parsed the YAML (and enrich_job calls this several times per job),
    which cost ~125 ms per job and made big searches take many minutes.
    """
    path = settings.BASE_DIR / filename
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return default
    hit = _FILE_CACHE.get(filename)
    if hit and hit[0] == str(path) and hit[1] == mtime:
        return hit[2]
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=_Loader) or default
    except (OSError, yaml.YAMLError):
        data = default
    _FILE_CACHE[filename] = (str(path), mtime, data)
    return data


def companies():
    return _load("companies.yaml", {}).get("companies", [])


def company_index():
    cs = companies()
    hit = _INDEX_CACHE.get("idx")
    if hit and hit[0] is cs:
        return hit[1]
    idx = {str(c.get("name", "")).casefold(): c for c in cs if c.get("name")}
    _INDEX_CACHE["idx"] = (cs, idx)
    return idx


def functions():
    return _load("job_functions.yaml", {}).get("job_functions", {})


def industries():
    return _load("industries.yaml", {}).get("industries", {})


def industries_catalog():
    """Return industry keys with human-readable labels for the search UI."""
    data = industries()
    return {key: str(key).replace("_", " ").title() for key in data}


def classify_job(title, description=""):
    """Return the best matching job-function keys from title + description."""
    text = f"{title or ''} {description or ''}".casefold()
    scores = []
    for key, data in functions().items():
        score = 0
        for keyword in data.get("keywords", []):
            k = str(keyword).casefold()
            if k in text:
                score += 3 if k in (title or "").casefold() else 1
        if score:
            scores.append((score, key))
    scores.sort(reverse=True)
    return [key for _, key in scores[:3]] or ["other"]


def classify_industry(title, description=""):
    text = f"{title or ''} {description or ''}".casefold()
    found = []
    for key, keywords in industries().items():
        if any(str(k).casefold() in text for k in keywords):
            found.append(key)
    return found[:3] or ["technology"]


def function_labels(keys):
    data = functions()
    return [data[k].get("label", k) for k in keys if k in data]


def industry_labels(keys):
    return [str(k).replace("_", " ").title() for k in keys]


def enrich_job(job):
    """Attach company metadata and lightweight function/industry classification."""
    index = company_index()
    company = index.get(str(job.get("company", "")).casefold(), {})
    job["company_regions"] = company.get("regions", [])
    job["company_industries"] = company.get("industries", [])
    job["company_job_functions"] = company.get("job_functions", [])
    job["job_functions"] = classify_job(job.get("title"), job.get("description"))
    job["job_industries"] = classify_industry(job.get("title"), job.get("description"))
    job["job_function_labels"] = function_labels(job["job_functions"])
    job["job_industry_labels"] = industry_labels(job["job_industries"])
    return job


def validate_registry():
    """Return simple diagnostics used by tests/management scripts."""
    cs = companies()
    names = [str(c.get("name", "")).strip().casefold() for c in cs]
    duplicates = sorted({n for n in names if n and names.count(n) > 1})
    invalid = []
    for c in cs:
        ats = c.get("ats")
        if ats not in {"greenhouse", "lever", "ashby", "workable", "jsonld"}:
            invalid.append((c.get("name"), "ats"))
        if not c.get("name") or not (c.get("token") or c.get("url")):
            invalid.append((c.get("name"), "source"))
    return {"companies": len(cs), "duplicates": duplicates, "invalid": invalid}
