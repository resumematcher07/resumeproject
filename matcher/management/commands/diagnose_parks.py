"""Find out exactly why a park (Infopark / Technopark / Cyberpark) shows no jobs.

    python manage.py diagnose_parks                  # try every park, print what each strategy found
    python manage.py diagnose_parks infopark         # one park
    python manage.py diagnose_parks --render         # also use a real headless browser (needs: pip install playwright && playwright install chromium)
    python manage.py diagnose_parks --save diag      # keep the raw HTML in ./diag/ (send it along if a park still fails)
    python manage.py diagnose_parks --html page.html --park infopark   # test the parser on a page you saved from your browser
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from matcher.services import park_sources as ps


class Command(BaseCommand):
    help = "Show how each IT-park listing page is read, and why it fails when it does."

    def add_arguments(self, parser):
        parser.add_argument("parks", nargs="*", help="park keys (default: all)")
        parser.add_argument("--render", action="store_true", help="also try a headless browser")
        parser.add_argument("--save", help="directory to save the downloaded HTML")
        parser.add_argument("--html", help="parse a saved HTML file instead of downloading")
        parser.add_argument("--park", default="infopark", help="park key used with --html")
        parser.add_argument("--show", type=int, default=8, help="how many jobs to print per park")

    def handle(self, *a, **o):
        if o["html"]:
            html = Path(o["html"]).read_text(encoding="utf-8", errors="ignore")
            base = ps.PARKS[o["park"]]["urls"][0]
            jobs = (ps.jobs_from_jsonld(html, o["park"], base) or ps.jobs_from_embedded(html, o["park"], base)
                    or ps.jobs_from_html(html, o["park"], base))
            self._print(o["park"], jobs, ["parsed local file"], o["show"])
            return
        keys = o["parks"] or list(ps.PARKS)
        save = Path(o["save"]) if o["save"] else None
        bad = 0
        for k in keys:
            if k not in ps.PARKS:
                self.stderr.write(f"unknown park {k!r}; choose from {', '.join(ps.PARKS)}")
                continue
            jobs, rep = ps.fetch_park_report(k, save_dir=save, force_render=False)
            if not jobs and o["render"]:
                jobs, rep2 = ps.fetch_park_report(k, save_dir=save, force_render=True)
                rep["notes"] += rep2["notes"]
            self._print(k, jobs, rep["notes"], o["show"])
            bad += not jobs
        if bad:
            self.stdout.write(self.style.WARNING(
                "\nHints: 'HTTP 403/429' = the site blocks this server (try from your PC, or use --render); 'JavaScript-rendered' = use --render; "
                "'Timeout/ConnectError' = outbound access is restricted (PythonAnywhere free accounts can only reach whitelisted sites). "
                "If it still fails, run with --save diag and send the files in ./diag/."))

    def _print(self, key, jobs, notes, show):
        head = f"{key}: {len(jobs)} jobs"
        self.stdout.write(self.style.SUCCESS(head) if jobs else self.style.ERROR(head))
        for n in notes:
            self.stdout.write(f"   - {n}")
        for j in sorted(jobs, key=lambda x: x["posted"], reverse=True)[:show]:
            self.stdout.write(f"   * {j['posted'][:10] or '----------'}  {j['title'][:60]:60}  {j['company'][:30]}")
