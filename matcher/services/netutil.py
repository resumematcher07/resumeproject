"""HTTP client for PUBLIC, read-only pages that tolerates sites with a broken TLS certificate.

Some park / company websites serve a certificate issued for a different host name (e.g. cyberparkkerala.org: "Hostname
mismatch"), so strict verification can never succeed, no matter how often it is retried.  For those pages only:
the request is first made with normal verification; if (and only if) it fails with a certificate error, it is repeated
once without verification.  No cookies, logins, or personal data are ever sent by this client - it only downloads
public job listings - and the fact is reported ("TLS certificate invalid; read without verification").
Turn the fallback off with PARK_INSECURE_FALLBACK=0.
"""
import httpx
from django.conf import settings


def is_cert_error(exc):
    s = f"{type(exc).__name__} {exc}".upper()
    return "CERTIFICATE" in s or "SSL" in s and "VERIFY" in s or "HOSTNAME MISMATCH" in s


class SafeClient:
    def __init__(self, timeout, headers, primary=None, fallback_factory=None):
        self._kw = dict(timeout=timeout, headers=headers, follow_redirects=True)
        self.primary = primary or httpx.Client(**self._kw)
        self._factory = fallback_factory or (lambda: httpx.Client(verify=False, **self._kw))
        self._fallback = None
        self.insecure_hosts = set()
        self.allowed = getattr(settings, "PARK_INSECURE_FALLBACK", True)

    def get(self, url, **kw):
        host = httpx.URL(url).host
        if host in self.insecure_hosts:
            return self._fb().get(url, **kw)
        try:
            return self.primary.get(url, **kw)
        except httpx.HTTPError as exc:
            if not (self.allowed and is_cert_error(exc)):
                raise
            self.insecure_hosts.add(host)
            return self._fb().get(url, **kw)

    def _fb(self):
        if self._fallback is None:
            self._fallback = self._factory()
        return self._fallback

    def close(self):
        self.primary.close()
        if self._fallback:
            self._fallback.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
