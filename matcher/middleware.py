import time

from .models import purge_expired

_last = 0.0


class PurgeExpiredMiddleware:
    """Safety net: even without the background thread/cron, expired resumes get wiped on traffic."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        global _last
        if time.time() - _last > 30:
            _last = time.time()
            try:
                purge_expired()
            except Exception:
                pass
        return self.get_response(request)


class NoStoreMiddleware:
    """Resume pages must never be kept by the browser/proxy cache, so the Back button cannot re-show a deleted resume."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith(("/static/", "/admin/static/")):
            response["Cache-Control"] = "no-store, private, max-age=0"
            response["Pragma"] = "no-cache"
            response["Referrer-Policy"] = "same-origin"
        return response
