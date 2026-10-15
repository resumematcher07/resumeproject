"""Show how ONE career page / registry entry is read - run this first when you add a company.

    python manage.py check_page "Bank of America"        # an entry already in companies.yaml
    python manage.py check_page --url https://careers.blackrock.com/search-jobs --ats html --link-pattern /job/ --page-param p --page-start 1

Run it on your own PC. It prints the jobs found (title / place / date / link) and the first job's detail text.
"""
import httpx
from django.core.management.base import BaseCommand, CommandError

from matcher.services import jobs as jm


class Command(BaseCommand):
    help = "Read one career page and print the jobs and the first job's description."

    def add_arguments(self, p):
        p.add_argument("name", nargs="?", help="company name in companies.yaml")
        p.add_argument("--url"); p.add_argument("--ats", default="html")
        p.add_argument("--link-pattern", default="/job"); p.add_argument("--page-param")
        p.add_argument("--page-start", type=int, default=0); p.add_argument("--page-step", type=int, default=1)
        p.add_argument("--pages", type=int, default=1); p.add_argument("--domain", default="")
        p.add_argument("--save-html", help="also save the first page's raw HTML to this file")

    def handle(self, *a, **o):
        if o["name"]:
            c = next((x for x in jm.load_companies() if x["name"].casefold() == o["name"].casefold()), None)
            if not c:
                raise CommandError(f"{o['name']!r} is not in companies.yaml")
            c = dict(c, max_pages=min(int(c.get("max_pages", 1)), o["pages"]))
        elif o["url"]:
            c = {"name": "test", "ats": o["ats"], "url": o["url"], "link_pattern": o["link_pattern"], "page_param": o["page_param"],
                 "page_start": o["page_start"], "page_step": o["page_step"], "max_pages": o["pages"], "domain": o["domain"]}
        else:
            raise CommandError("give a company name or --url")
        with httpx.Client(timeout=30, headers=jm.UA, follow_redirects=True) as client:
            if o["save_html"] and c.get("url"):
                open(o["save_html"], "w", encoding="utf-8").write(client.get(c["url"]).text)
            try:
                found = list(jm.READERS[c["ats"]](c, client))
            except Exception as exc:
                raise CommandError(f"{type(exc).__name__}: {exc}")
        self.stdout.write(f"{len(found)} jobs read from {c.get('url') or c.get('token')}")
        for j in found[:15]:
            self.stdout.write(f"  {j['title'][:55]:55} | {j['location'][:28]:28} | {j['posted'][:10] or '-':10} | {j['url']}")
        if not found:
            self.stderr.write("NOTHING FOUND: the page probably builds its list with JavaScript (use --save-html and send it for a custom reader), or --link-pattern does not match the job links.")
            return
        first = jm.fetch_detail(found[0])
        self.stdout.write(f"---- description of: {first['title']} ({len(first['description'])} chars) ----")
        self.stdout.write(first["description"][:700] or "(empty - detail page needs JavaScript or blocks us)")
