"""Download the newest job snapshot from GitHub (github.com / githubusercontent.com are on PythonAnywhere's free allow-list).

    python manage.py pull_snapshot

Set SNAPSHOT_URL (env var) or pass --url, e.g.
    https://raw.githubusercontent.com/<you>/<repo>/data/jobs_snapshot.json.gz
"""
import gzip
import json
import os

import httpx
from django.core.management.base import BaseCommand, CommandError

from matcher.services import snapshot


class Command(BaseCommand):
    help = "Download jobs_snapshot.json.gz from GitHub and install it atomically."

    def add_arguments(self, parser):
        parser.add_argument("--url", default=os.environ.get("SNAPSHOT_URL", ""))

    def handle(self, *args, **o):
        url = o["url"]
        if not url:
            raise CommandError("Set SNAPSHOT_URL or pass --url")
        try:
            r = httpx.get(url, timeout=60, follow_redirects=True)
            r.raise_for_status()
        except Exception as exc:
            raise CommandError(f"Download failed: {type(exc).__name__}: {exc}")
        try:                                               # validate BEFORE replacing the working file
            data = json.loads(gzip.decompress(r.content).decode("utf-8"))
            n = len(data["jobs"])
        except Exception:
            raise CommandError("Downloaded file is not a valid snapshot - keeping the old one.")
        if n == 0:
            raise CommandError("Snapshot has 0 jobs - keeping the old one.")
        path = snapshot.snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        os.replace(tmp, path)
        self.stdout.write(self.style.SUCCESS(f"installed snapshot with {n} jobs ({len(r.content) / 1024:.0f} KB)"))
