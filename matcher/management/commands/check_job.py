"""Check how ONE job page is read - the quickest way to see why a requirement does not show up.

    python manage.py check_job https://infopark.in/jobs/mozilor-technologies-pvt-ltd/meta-ads-specialist-1
    python manage.py check_job <url> --title "Meta Ads Specialist"

Run it on your own PC (PythonAnywhere free cannot open most career sites).
"""
import re

from django.core.management.base import BaseCommand, CommandError

from matcher.services import analysis
from matcher.services import jobs as jobs_mod
from matcher.services.netutil import SafeClient


class Command(BaseCommand):
    help = "Download one job page and show the text, years of experience and skills the app reads from it."

    def add_arguments(self, parser):
        parser.add_argument("url")
        parser.add_argument("--title", default="", help="Job title as shown on the page (cuts the site menu off the text)")
        parser.add_argument("--chars", type=int, default=900, help="How much of the extracted text to print")

    def handle(self, *args, **o):
        try:
            with SafeClient(30, jobs_mod.UA) as client:
                r = client.get(o["url"])
                r.raise_for_status()
        except Exception as exc:
            raise CommandError(f"Could not download the page: {type(exc).__name__}: {exc}")
        title = o["title"]
        if not title:                                           # "Meta Ads Specialist Jobs at X | Site" -> "Meta Ads Specialist"
            m = re.search(r"<title>\s*(.+?)\s*</title>", r.text, re.I | re.S)
            title = re.split(r"\s+Jobs?\b|\s*\|\s*|\s+-\s+", m.group(1))[0].strip() if m else ""
        text = jobs_mod.park_page_text(r.text, title)
        req = analysis.compute_requirements({"title": title, "description": text})
        self.stdout.write(f"title used     : {title or '(none found - pass --title)'}")
        self.stdout.write(f"text length    : {len(text)} characters, {text.count(chr(10)) + 1} lines")
        self.stdout.write(f"years of exp.  : {req['years'] or 'not stated'}  ({req['years_src'] or 'no source'})")
        self.stdout.write(f"fresher-friendly: {req['fresher']}")
        self.stdout.write(f"required skills: {', '.join(req['required']) or '-'}")
        self.stdout.write(f"preferred      : {', '.join(req['preferred']) or '-'}")
        self.stdout.write("---- text the app reads (start) ----")
        self.stdout.write(text[: o["chars"]])
        if len(text) < 200:
            self.stderr.write("WARNING: very little text. The page may need JavaScript (the scraper then needs Playwright) or the title does not match.")
