"""Job snapshot: scrape on GitHub Actions, serve on PythonAnywhere free.

PythonAnywhere free accounts cannot reach arbitrary career sites, so the scraping runs somewhere else
(a scheduled GitHub Actions workflow) and writes ONE compressed file, jobs_snapshot.json.gz.
The web app only ever reads that file (JOB_SOURCE=snapshot) - it makes no outgoing scraping requests.

No resume data is ever part of the snapshot. It contains public job postings only.
"""
import copy
import gzip
import json
import os
import threading
import time
from pathlib import Path

from django.conf import settings

_lock = threading.Lock()
_mem = {"mtime": None, "data": None}


def snapshot_path():
    return Path(getattr(settings, "JOB_SNAPSHOT_FILE", settings.BASE_DIR / "data" / "jobs_snapshot.json.gz"))


def enabled():
    return getattr(settings, "JOB_SOURCE", "live") == "snapshot"


# ---------------------------------------------------------------- write side (GitHub Actions / your PC)

def write_snapshot(jobs, errors, n_companies, parks, path=None):
    """parks = {key: {"jobs": [...], "report": {...}}}"""
    path = Path(path or snapshot_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "generated_at": time.time(),
        "n_companies": n_companies,
        "errors": errors,
        "jobs": jobs,
        "parks": parks,
    }
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)          # atomic: a reader never sees a half-written file
    return path


# ---------------------------------------------------------------- read side (PythonAnywhere)

def load():
    """Return the snapshot dict (cached in memory; re-read only if the file changed). Empty snapshot if missing."""
    path = snapshot_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {"jobs": [], "errors": ["Job snapshot not found - run update_snapshot"], "n_companies": 0, "parks": {}, "generated_at": 0}
    with _lock:
        if _mem["mtime"] != mtime:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                _mem["data"] = json.load(fh)
            _mem["mtime"] = mtime
        return _mem["data"]


def jobs_copy():
    """Fresh copies, because the ranking code adds fields to job dicts."""
    return [dict(j) for j in load()["jobs"]]


def park(key):
    p = load().get("parks", {}).get(key) or {"jobs": [], "report": None}
    return copy.deepcopy(p["jobs"]), p["report"]


def age_hours():
    g = load().get("generated_at") or 0
    return round((time.time() - g) / 3600, 1) if g else None
