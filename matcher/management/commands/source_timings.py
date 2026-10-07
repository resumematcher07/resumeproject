"""Show which career sources are slow or dead on THIS machine/network.

    python manage.py source_timings                   table sorted slowest-first
    python manage.py source_timings --quarantine 8    sources slower than 8 s or failing are skipped by searches for 6 h

Searches are only as fast as the sources they have to wait for; this tells you exactly which ones to remove from
companies.yaml (or quarantine) instead of guessing.
"""
import concurrent.futures as cf
import time

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand

from matcher.services import jobs


def _timed(c):
    t = time.time()
    got, err = jobs._fetch_live(c)
    return c, len(got), err, time.time() - t


class Command(BaseCommand):
    help = "Time every company career source."

    def add_arguments(self, parser):
        parser.add_argument("--workers", type=int, default=24)
        parser.add_argument("--quarantine", type=float, default=0, metavar="SECONDS")
        parser.add_argument("--top", type=int, default=25)

    def handle(self, *args, **opts):
        companies = jobs.load_companies()
        self.stdout.write(f"Timing {len(companies)} sources...")
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=opts["workers"]) as pool:
            rows = sorted(pool.map(_timed, companies), key=lambda r: -r[3])
        ok = [r for r in rows if not r[2]]
        self.stdout.write(f"Done in {time.time() - t0:.0f}s: {len(ok)} reachable, {len(rows) - len(ok)} failed, "
                          f"{sum(1 for r in ok if r[1] == 0)} reachable but returned no jobs.")
        self.stdout.write(f"\nSlowest {opts['top']}:")
        for c, n, err, dt in rows[:opts["top"]]:
            self.stdout.write(f"  {dt:5.1f}s  {c['name']:<28} {c['ats']:<10} {'FAILED ' + err if err else str(n) + ' jobs'}")
        if opts["quarantine"]:
            skipped = 0
            for c, n, err, dt in rows:
                if err or dt > opts["quarantine"]:
                    cache.set(jobs._source_key(c) + ":err", f"{c['name']}: skipped (slow/dead)", settings.JOB_EMPTY_CACHE_SECONDS)
                    skipped += 1
            self.stdout.write(self.style.SUCCESS(f"Quarantined {skipped} slow/failed sources for {settings.JOB_EMPTY_CACHE_SECONDS // 3600} h."))
