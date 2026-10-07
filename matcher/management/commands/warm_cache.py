"""Pre-fetch every company career source so searches are instant.

    python manage.py warm_cache            one round, then exit (good for a scheduled task)
    python manage.py warm_cache --loop     keep refreshing forever (run next to the web server)
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from matcher.services import jobs


class Command(BaseCommand):
    help = "Fetch all company career sources into the shared cache."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--workers", type=int, default=24)

    def handle(self, *args, **opts):
        while True:
            t = time.time()
            all_jobs, errors, total = jobs.fetch_all(workers=opts["workers"], refresh=True)
            self.stdout.write(self.style.SUCCESS(
                f"{total} sources, {len(all_jobs)} jobs cached, {len(errors)} unreachable, {time.time() - t:.0f}s"))
            if not opts["loop"]:
                return
            time.sleep(settings.JOB_WARM_INTERVAL)
