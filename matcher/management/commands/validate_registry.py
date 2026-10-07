"""Validate the company career-source registry.

    python manage.py validate_registry                       offline schema/duplicate checks (original behaviour)
    python manage.py validate_registry --live                also contact every source and report the dead ones
    python manage.py validate_registry --live --prune        ...and delete dead greenhouse/lever/ashby/workable lines
    python manage.py validate_registry --candidates          check companies_candidates.yaml, append only the ones
                                                             that really respond to companies.yaml (auto-fixing a
                                                             wrong ATS / token guess when another one matches)

Network access to boards-api.greenhouse.io, api.lever.co, api.ashbyhq.com and apply.workable.com is required for
the live modes. companies.yaml keeps its comments: new entries are appended and pruned entries removed line by line.
"""
import concurrent.futures as cf
import re
import shutil
from datetime import date

import httpx
import yaml
from django.conf import settings
from django.core.management.base import BaseCommand

from matcher.services.catalog import validate_registry
from matcher.services.jobs import READERS, UA

PROBE_ORDER = ("greenhouse", "lever", "ashby", "workable")
SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|pvt|private|plc|gmbh|ag|sa|co|corp|corporation|group|technologies|"
                    r"technology|solutions|labs)\b")


def slugs(name, token=None):
    """Plausible board slugs for a company name (full-name variants only, to avoid look-alike collisions)."""
    base = re.sub(r"\(.*?\)", "", name or "").lower().replace("&", "and")
    out = [token] if token else []
    for text in (base, SUFFIX.sub("", base)):
        s = re.sub(r"[^a-z0-9 ]", "", text).strip()
        if s:
            out += [s.replace(" ", ""), s.replace(" ", "-")]
    seen, res = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            res.append(s)
    return res


def probe(entry):
    """Run the project's own reader on one source. Returns (job_count, sample_url, error)."""
    timeout = httpx.Timeout(10.0, connect=5.0)
    try:
        with httpx.Client(timeout=timeout, headers=UA, follow_redirects=True) as client:
            jobs = [j for j in READERS[entry["ats"]](entry, client) if j["title"] and j["url"]]
        return len(jobs), (jobs[0]["url"] if jobs else ""), None
    except Exception as exc:
        return 0, "", type(exc).__name__


def resolve(entry):
    """Verify a candidate; if its guessed ats/token fails, try other slugs and ATSes. Returns (entry, jobs, url)."""
    n, url, err = probe(entry)
    if err is None:
        return entry, n, url
    for slug in slugs(entry["name"], entry.get("token")):
        for ats in PROBE_ORDER:
            if ats == entry["ats"] and slug == entry.get("token"):
                continue
            trial = {**entry, "ats": ats, "token": slug}
            n, url, err = probe(trial)
            if err is None and n > 0:               # a rediscovered board must actually list jobs
                return trial, n, url
    return None, 0, ""


def fmt(entry):
    return "  - " + yaml.dump(entry, default_flow_style=True, width=10 ** 6, sort_keys=False,
                             allow_unicode=True).strip()


class Command(BaseCommand):
    help = "Validate the company career-source registry (offline by default, --live to contact the sites)."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true", help="contact every source in companies.yaml")
        parser.add_argument("--prune", action="store_true", help="with --live: remove dead ATS sources (backup kept)")
        parser.add_argument("--include-jsonld", action="store_true", help="with --prune: also drop jsonld pages with no JobPosting data")
        parser.add_argument("--candidates", nargs="?", const="companies_candidates.yaml", metavar="FILE",
                            help="verify FILE and append the working companies to companies.yaml")
        parser.add_argument("--workers", type=int, default=32)

    # ---- original offline checks (unchanged) ----
    def _offline(self):
        result = validate_registry()
        self.stdout.write(f"Companies: {result['companies']}")
        if result["duplicates"]:
            self.stdout.write(self.style.WARNING("Duplicate names: " + ", ".join(result["duplicates"])))
        if result["invalid"]:
            for name, reason in result["invalid"]:
                self.stdout.write(self.style.ERROR(f"Invalid {reason}: {name}"))
        else:
            self.stdout.write(self.style.SUCCESS("Registry schema checks passed."))

    def handle(self, *args, **options):
        self._offline()
        if options["live"]:
            self._live(options)
        if options["candidates"]:
            self._candidates(options)

    # ---- live check of the current registry ----
    def _live(self, options):
        path = settings.COMPANIES_FILE
        companies = yaml.safe_load(path.read_text(encoding="utf-8")).get("companies", [])
        self.stdout.write(f"Contacting {len(companies)} sources ({options['workers']} at a time)...")
        dead, empty = [], []
        with cf.ThreadPoolExecutor(max_workers=options["workers"]) as pool:
            for c, (n, _url, err) in zip(companies, pool.map(probe, companies)):
                if err:
                    dead.append((c, err))
                elif n == 0:
                    empty.append(c)
        ok = len(companies) - len(dead)
        self.stdout.write(self.style.SUCCESS(f"Reachable: {ok}/{len(companies)}"))
        for c, err in dead:
            self.stdout.write(self.style.ERROR(f"  DEAD  {c['name']} ({c['ats']}:{c.get('token') or c.get('url')}) {err}"))
        if empty:
            self.stdout.write(self.style.WARNING("Reachable but no jobs listed right now: " + ", ".join(c["name"] for c in empty)))
        if options["prune"]:
            drop = {c["name"] for c, _ in dead if c["ats"] != "jsonld" or options["include_jsonld"]}
            if options["include_jsonld"]:
                drop |= {c["name"] for c in empty if c["ats"] == "jsonld"}
            self._remove_lines(path, drop)

    def _remove_lines(self, path, names):
        if not names:
            self.stdout.write("Nothing to prune.")
            return
        shutil.copy(path, str(path) + ".bak")
        kept, removed = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("- {"):
                try:
                    if yaml.safe_load(s[2:]).get("name") in names:
                        removed += 1
                        continue
                except yaml.YAMLError:
                    pass
            kept.append(line)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Pruned {removed} dead sources (backup: {path.name}.bak)."))

    # ---- promote verified candidates ----
    def _candidates(self, options):
        cand_path = settings.BASE_DIR / options["candidates"]
        path = settings.COMPANIES_FILE
        cands = yaml.safe_load(cand_path.read_text(encoding="utf-8")).get("companies", [])
        existing = yaml.safe_load(path.read_text(encoding="utf-8")).get("companies", [])
        names = {str(c.get("name", "")).casefold() for c in existing}
        sources = {(c.get("ats"), c.get("token") or c.get("url")) for c in existing}
        todo = [c for c in cands if str(c.get("name", "")).casefold() not in names]
        self.stdout.write(f"Checking {len(todo)} candidates ({len(cands) - len(todo)} already registered)...")
        added, failed, report = [], [], []
        with cf.ThreadPoolExecutor(max_workers=options["workers"]) as pool:
            for c, (fixed, n, url) in zip(todo, pool.map(resolve, todo)):
                if not fixed or (fixed["ats"], fixed["token"]) in sources:
                    failed.append(c["name"])
                    continue
                sources.add((fixed["ats"], fixed["token"]))
                added.append(fixed)
                report.append(f"{fixed['name']}\t{fixed['ats']}:{fixed['token']}\t{n} jobs\t{url}")
        if added:
            block = ["", f"  # ===== ADDED AND LIVE-VERIFIED {date.today().isoformat()} ====="] + [fmt(e) for e in added]
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(block) + "\n")
        (settings.BASE_DIR / "registry_report.txt").write_text(
            "company\tsource\tjobs\tsample job URL (check it belongs to the right company)\n" + "\n".join(report) + "\n",
            encoding="utf-8")
        (settings.BASE_DIR / "companies_unreachable.txt").write_text("\n".join(failed) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Added {len(added)} working companies to {path.name}."))
        self.stdout.write(f"{len(failed)} not reachable on Greenhouse/Lever/Ashby/Workable -> companies_unreachable.txt")
        self.stdout.write("Skim registry_report.txt once: look-alike board names can belong to a different company.")
