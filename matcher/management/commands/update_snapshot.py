"""Scrape every career source + official IT-park listing and write data/jobs_snapshot.json.gz.

Run this where the internet is unrestricted (GitHub Actions, or your own PC) - NOT on PythonAnywhere free:

    python manage.py update_snapshot
"""
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from matcher.services import jobs as jobs_mod
from matcher.services import park_sources, snapshot


class Command(BaseCommand):
    help = "Scrape all job sources and write the compressed snapshot file used by the PythonAnywhere site."

    def add_arguments(self, parser):
        parser.add_argument("--out", help="Output file (default: data/jobs_snapshot.json.gz)")
        parser.add_argument("--deadline", type=int, default=300, help="Max seconds to wait for all career sites")
        parser.add_argument("--detail-limit", type=int, default=2500,
                            help="Max job descriptions to download for listings that only expose titles (newest first)")
        parser.add_argument("--description-chars", type=int, default=8000, help="Trim each description to this many characters")
        parser.add_argument("--force", action="store_true", help="Write even if far fewer jobs than the previous snapshot")

    def handle(self, *args, **o):
        settings.JOB_SOURCE = "live"                       # the scraper must really go online
        settings.JOB_SEARCH_DEADLINE = o["deadline"]
        settings.PARK_RENDER = True                        # use headless Chromium for JavaScript-only park pages if installed
        t0 = time.time()

        self.stdout.write("1/4 career sites ...")
        all_jobs, errors, n_companies = jobs_mod.fetch_all(workers=48, refresh=True)
        self.stdout.write(f"    {len(all_jobs)} jobs from {n_companies} sources, {len(errors)} unreachable")

        self.stdout.write("2/4 official IT-park listings ...")
        parks = {}
        for key in park_sources.PARKS:
            try:
                pj, report = park_sources.fetch_park_report(key)
            except Exception as exc:                       # one broken park must not abort the whole run
                pj, report = [], {"park": key, "name": park_sources.PARKS[key]["name"], "jobs": 0, "ok": False,
                                  "notes": [f"{type(exc).__name__}: {str(exc)[:150]}"]}
            parks[key] = {"jobs": pj, "report": report}
            self.stdout.write(f"    {key}: {len(pj)} jobs")

        self.stdout.write("3/4 job descriptions ...")
        everything = all_jobs + [j for p in parks.values() for j in p["jobs"]]
        todo = [j for j in everything if j.get("detail") and not j.get("description")]
        todo.sort(key=lambda j: j.get("posted") or "", reverse=True)
        jobs_mod.fetch_details(todo[: o["detail_limit"]], workers=32)
        cap = o["description_chars"]
        for j in everything:
            j["description"] = (j.get("description") or "")[:cap]
            j.pop("detail", None)                          # snapshot jobs are complete; nothing left to fetch later

        # Safety: never replace a good snapshot with a broken one (e.g. GitHub was rate-limited / network down).
        out = o["out"] or snapshot.snapshot_path()
        try:
            previous = len(snapshot.load()["jobs"]) if not o["out"] else 0
        except Exception:
            previous = 0
        if not o["force"] and (not all_jobs or (previous and len(all_jobs) < previous * 0.5)):
            raise CommandError(f"Only {len(all_jobs)} jobs scraped vs {previous} in the previous snapshot - "
                               "refusing to overwrite it (use --force to override).")

        self.stdout.write("4/4 writing snapshot ...")
        path = snapshot.write_snapshot(all_jobs, errors, n_companies, parks, out)
        size = path.stat().st_size / 1024 / 1024
        self.stdout.write(self.style.SUCCESS(f"done in {time.time() - t0:.0f}s -> {path} ({size:.1f} MB)"))
        if size > 90:
            self.stderr.write("WARNING: snapshot is over 90 MB; GitHub rejects files above 100 MB. Lower --description-chars.")
            sys.exit(1)
