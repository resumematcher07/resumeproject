import time

from django.core.management.base import BaseCommand

from matcher.models import purge_expired


class Command(BaseCommand):
    help = "Delete resumes (and generated files) older than the TTL. Use --loop to run forever, or cron it."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run continuously, once a minute")

    def handle(self, *args, **opts):
        while True:
            n = purge_expired()
            self.stdout.write(f"purged {n} expired resume(s)")
            if not opts["loop"]:
                return
            time.sleep(60)
