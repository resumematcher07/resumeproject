import os
import sys
import threading
import time

from django.apps import AppConfig


class MatcherConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "matcher"

    def ready(self):
        from django.conf import settings

        argv = " ".join(sys.argv)
        serving = ("runserver" in argv and os.environ.get("RUN_MAIN") == "true") or "manage.py" not in argv
        if not (settings.RESUME_START_PURGER and serving) or "pytest" in argv or "test" in sys.argv[1:2]:
            return
        threading.Thread(target=self._loop, daemon=True, name="resume-purger").start()
        if settings.JOB_START_WARMER:
            threading.Thread(target=self._warm_loop, daemon=True, name="job-warmer").start()

    @staticmethod
    def _warm_loop():
        """Keep every career source cached so a search reads from disk instead of waiting on 200+ websites."""
        from django.conf import settings

        from .services import jobs

        time.sleep(int(__import__("os").environ.get("JOB_WARM_DELAY", 15)))   # let the site finish starting first
        while True:
            try:
                jobs.fetch_all(workers=16, refresh=True)
            except Exception:
                pass
            time.sleep(settings.JOB_WARM_INTERVAL)

    @staticmethod
    def _loop():
        from django.conf import settings

        from .models import purge_expired

        while True:
            time.sleep(settings.RESUME_PURGE_INTERVAL)
            try:
                purge_expired()
            except Exception:       # DB may be mid-migration / locked; try again next tick
                pass
