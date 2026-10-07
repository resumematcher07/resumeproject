import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone as dj_tz

from .models import GeneratedResume, Resume, purge_expired
from .services import analysis, jobs, latex, parser

# tests never touch the network: park listings default to 'nothing found' unless a test supplies them
analysis.fetch_park_jobs = lambda keys, progress=None: ([], [], len(keys))

RESUME = """Jane Doe
Senior Python Developer
jane.doe@example.com | +1 415 555 0134 | github.com/janedoe

SUMMARY
Backend engineer with 6 years of experience building web services and data pipelines.

SKILLS
Python, Django, Flask, PostgreSQL, Docker, REST APIs, Git, Agile, Redis

EXPERIENCE
Senior Python Developer - Acme Corp
Jan 2021 - Present
- Built Django REST APIs serving 2M requests a day
- Migrated services to Docker and cut deploy time by 60%
Software Engineer, Beta Ltd
Jun 2018 - Dec 2020
- Developed Flask microservices backed by PostgreSQL and Redis

EDUCATION
B.Tech in Computer Science, State University, 2018

CERTIFICATIONS
- Certified ScrumMaster (CSM)

PROJECTS
Job Tracker
- Django app with Docker deployment and CI/CD using GitHub Actions
""" + "\nExtra filler line to ensure enough words are present for parsing." * 3


def _job(title, company, desc, days_ago):
    posted = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"company": company, "title": title, "url": f"https://x.test/{company}/{title}".replace(" ", "-"),
            "location": "Remote", "posted": posted.isoformat(), "description": desc}


JOBS = [
    _job("Senior Python Engineer", "OldCo", "Requirements: 5+ years of experience. Python, Django, PostgreSQL, Docker. "
         "Nice to have: Kubernetes.", 20),
    _job("Python Developer", "NewCo", "You will use Python, Django, AWS, Kubernetes and Terraform. "
         "AWS Certified Solutions Architect required. 10+ years experience.", 1),
    _job("Head Chef", "Kitchen", "Cook food. Manage kitchen.", 0),
    _job("Backend Engineer", "MidCo", "Python, Flask, Redis, PostgreSQL, Git. 3 years experience.", 5),
]


class ParserTests(TestCase):
    def test_parse(self):
        p = parser.parse_resume(RESUME)
        self.assertEqual(p["name"], "Jane Doe")
        self.assertEqual(p["email"], "jane.doe@example.com")
        self.assertEqual(p["latest_title"], "Senior Python Developer")
        self.assertIn("Django", p["skills_flat"])
        self.assertIn("Docker", p["skills_flat"])
        self.assertNotIn("Go", p["skills_flat"])
        self.assertGreaterEqual(p["years_experience"], 6)
        self.assertTrue(p["education"] and p["projects"] and p["certifications"])
        self.assertIn("Certified ScrumMaster (CSM)", p["certs_detected"])

    def test_special_skills(self):
        p = parser.parse_resume("Worked with C++, C#, Node.js and .NET plus Go and R.\n" * 5)
        for s in ("C++", "C#", "Node.js", ".NET", "Go", "R"):
            self.assertIn(s, p["skills_flat"])
        self.assertNotIn("Java", p["skills_flat"])      # must not match inside JavaScript-less text


class AnalysisTests(TestCase):
    def setUp(self):
        self.p = parser.parse_resume(RESUME)

    def test_newest_first_and_irrelevant_filtered(self):
        with mock.patch.object(analysis, "fetch_all", return_value=(JOBS, [], 3)):
            results, stats = analysis.search_and_rank(self.p)
        titles = [r["title"] for r in results]
        self.assertNotIn("Head Chef", titles)
        self.assertEqual(titles[0], "Python Developer")          # posted 1 day ago
        self.assertEqual(titles[-1], "Senior Python Engineer")   # posted 20 days ago
        self.assertEqual(stats["jobs_scanned"], 4)

    def test_gaps(self):
        r = analysis.evaluate(self.p, JOBS[1])
        self.assertIn("AWS", r["missing_required"])
        self.assertTrue(any(c["name"] == "AWS Certified Solutions Architect" for c in r["missing_certs"]))
        self.assertEqual(r["years_needed"], 10)
        self.assertTrue(r["years_gap"] > 0)
        self.assertNotEqual(r["verdict"], "eligible")
        good = analysis.evaluate(self.p, JOBS[3])
        self.assertEqual(good["verdict"], "eligible")


@override_settings(RESUME_SYNC_SEARCH=True)
class FlowTests(TestCase):
    def _upload(self, text=RESUME, name="jane.txt"):
        return self.client.post(reverse("upload"), {"resume": SimpleUploadedFile(name, text.encode())})

    def test_full_flow_and_deletion(self):
        resp = self._upload()
        self.assertEqual(resp.status_code, 302)
        resume = Resume.objects.get()
        self.assertEqual(resume.original_name, "jane.txt")
        self.assertEqual(self.client.get(resp["Location"]).status_code, 200)

        with mock.patch.object(analysis, "fetch_all", return_value=(JOBS, [], 3)):
            r = self.client.post(reverse("search", args=[resume.pk]))
        self.assertRedirects(r, reverse("results", args=[resume.pk]))
        page = self.client.get(reverse("results", args=[resume.pk]))
        self.assertContains(page, "Python Developer")
        self.assertContains(page, "AWS Certified Solutions Architect")
        self.assertContains(page, "ATS resume")
        self.assertContains(self.client.get(reverse("detail", args=[resume.pk])), "role-head")

        resume.refresh_from_db()
        key = next(r["key"] for r in resume.analysis["results"] if r["company"] == "NewCo")
        dl = self.client.post(reverse("tailor", args=[resume.pk, key]), {"have_skill": ["AWS"], "pursuing_cert": ["AWS Certified Solutions Architect"]})
        self.assertEqual(dl.status_code, 200)
        body = dl.content
        from docx import Document
        text = "\n".join(p.text for p in Document(io.BytesIO(body)).paragraphs)
        self.assertIn("AWS", text)
        self.assertIn("(in progress)", text)
        self.assertNotIn("Terraform", text)                 # unconfirmed skills are never invented
        gen = GeneratedResume.objects.get()
        self.assertFalse(resume.file)                       # the uploaded original is never written to disk
        stored = [gen.file.path]

        # nobody else can see it
        other = self.client_class()
        self.assertEqual(other.get(reverse("detail", args=[resume.pk])).status_code, 404)

        # expiry removes DB rows AND files from disk
        Resume.objects.update(expires_at=dj_tz.now() - timedelta(seconds=1))
        self.assertEqual(purge_expired(), 1)
        self.assertEqual(Resume.objects.count() + GeneratedResume.objects.count(), 0)
        import os
        self.assertFalse(any(os.path.exists(p) for p in stored))

    def test_delete_now(self):
        self._upload()
        resume = Resume.objects.get()
        self.assertFalse(resume.file)
        self.client.post(reverse("delete", args=[resume.pk]))
        self.assertEqual(Resume.objects.count(), 0)

    def test_ttl_is_one_hour(self):
        self._upload()
        left = Resume.objects.get().seconds_left
        self.assertTrue(3590 <= left <= 3600)

    def test_bad_uploads(self):
        for name, data in [("x.exe", RESUME), ("short.txt", "too short")]:
            self.client.post(reverse("upload"), {"resume": SimpleUploadedFile(name, data.encode())})
        self.assertEqual(Resume.objects.count(), 0)


class FetcherTests(TestCase):
    def test_readers_parse_ats_payloads(self):
        class R:
            def __init__(s, j=None, t=""): s._j, s.text = j, t
            def raise_for_status(s): pass
            def json(s): return s._j
        class C:
            def __init__(s, resp): s.resp = resp
            def get(s, *a, **k): return s.resp
        gh = {"jobs": [{"title": "Dev", "absolute_url": "u", "location": {"name": "NY"}, "updated_at": "2026-09-30T10:00:00Z",
                        "content": "&lt;p&gt;Python &amp;amp; Django&lt;/p&gt;"}]}
        j = list(jobs._greenhouse({"name": "G", "token": "g"}, C(R(gh))))[0]
        self.assertEqual(j["company"], "G"); self.assertEqual(j["description"], ""); self.assertTrue(j["posted"].startswith("2026-09-30"))
        self.assertEqual(j["detail"]["token"], "g")          # light listing; full text is fetched later only if relevant
        with mock.patch.object(jobs.httpx, "Client") as cl:
            cl.return_value.__enter__.return_value.get.return_value = R({"content": "&lt;p&gt;Python &amp;amp; Django&lt;/p&gt;"})
            self.assertIn("Python", jobs.fetch_detail(j)["description"])
        lv = [{"text": "Dev", "hostedUrl": "u", "categories": {"location": "SF"}, "createdAt": 1790000000000, "descriptionPlain": "Go"}]
        self.assertTrue(list(jobs._lever({"name": "L", "token": "l"}, C(R(lv))))[0]["posted"])
        ab = {"jobs": [{"title": "Dev", "jobUrl": "u", "location": "X", "publishedAt": "2026-10-01", "descriptionPlain": "Rust"}]}
        self.assertEqual(list(jobs._ashby({"name": "A", "token": "a"}, C(R(ab))))[0]["title"], "Dev")
        wk = {"jobs": [{"title": "QA Engineer", "shortcode": "AB12", "url": "https://apply.workable.com/co/j/AB12/", "city": "Kochi",
                        "state": "Kerala", "country": "India", "published_on": "2026-10-03", "description": "<p>Selenium &amp; Python</p>"},
                       {"title": "Remote Dev", "url": "https://apply.workable.com/co/j/CD34/", "telecommuting": True,
                        "created_at": "2026-10-04T10:00:00Z", "description": ""}]}
        w = list(jobs._workable({"name": "W", "token": "co"}, C(R(wk))))
        self.assertEqual((w[0]["location"], w[0]["title"]), ("Kochi, Kerala, India", "QA Engineer"))
        self.assertTrue(w[0]["posted"].startswith("2026-10-03")); self.assertIn("Selenium", w[0]["description"])
        self.assertEqual(w[1]["location"], "Remote")
        html = '<script type="application/ld+json">{"@type":"JobPosting","title":"QA","datePosted":"2026-10-02","description":"<b>Selenium</b>","url":"/j/1"}</script>'
        out = list(jobs._jsonld({"name": "J", "url": "https://j.test/careers"}, C(R(t=html))))
        self.assertEqual(out[0]["url"], "https://j.test/j/1")


class BackgroundSearchTests(TransactionTestCase):   # thread uses its own DB connection -> needs committed data
    def test_async_search_reports_progress_then_results(self):
        import time
        self.client.post(reverse("upload"), {"resume": SimpleUploadedFile("j.txt", RESUME.encode())})
        resume = Resume.objects.get()
        with mock.patch.object(analysis, "fetch_all", return_value=(JOBS, [], 3)):
            r = self.client.post(reverse("search", args=[resume.pk]))
            self.assertEqual(r.json()["state"], "running")
            for _ in range(50):
                st = self.client.get(reverse("status", args=[resume.pk])).json()
                if st["state"] != "running":
                    break
                time.sleep(0.1)
        self.assertEqual(st["state"], "done")
        resume.refresh_from_db()
        self.assertEqual(len(resume.analysis["results"]), 3)


# ---------- anonymised copies of two real-world layouts ----------
KENZ_LIKE = (
    "Asha R Nair\nasha@example.com j +91-9000000001 j linkedin.com/in/asha-r-nair j Kerala\n"
    "SUMMARY\nEntry-level Data Scientist with hands-on experience in Python, SQL, and Machine Learning, specializing in\n"
    "predictive modeling and large-scale data analysis.\n"
    "EDUCATION\nSample College of Engineering 2021 - 2025\nBachelor of Technology in Computer Engineering, First Class\n"
    "TECHNICAL SKILLS\nLanguage: Python, SQL, MySQL, C\nData & Visualization:Power BI, Tableau, Pandas\n"
    "CERTIFICATIONS & COURSES\nData Fundamentals (IBM) j Data Science and Analytics Certi\x0ccation (HP\nLife) j SQL Analytics (Databricks)\n"
    "EXPERIENCE\nAcme Softech, Kochi| Data Science Trainee Aug 2025 - Jan 2026\n"
    "{ Performed data cleaning on 500K+ records monthly, and enabling accurate predictive modeling.\n"
    "{ Engineered XGBoost models to forecast prices, achieving 92% accuracy\nand enabling 15-20% pro\x0ct margin improvements for farmers.\n"
    "Webly, Kochi| Web Development Intern May 2023\n{ Built responsive pages using HTML, CSS, and PHP\n"
    "PROJECTS\nPrice Prediction SystemPython, XGBoost, Streamlit| Project-Link\n{ Designed a predictive analytics platform processing 36M+ records.\n"
    "COMMUNITY SERVICE & VOLUNTEER WORK\nNational Service Scheme (NSS)Student Volunteer Jul 2019 - Mar 2021\n"
)
ADITH_LIKE = (
    "ARUN K SAMUEL  \n+91 9000000002 | arun@example.com | linkedin.com/in/arunks \nKerala, India | Open to relocation \n"
    "PROFESSIONAL SUMMARY \nFinance and Business Analytics postgraduate with experience in business performance analysis, internal audit and \naccounting. Analyses financial data. \n"
    "PROFESSIONAL EXPERIENCE \nInternal Audit & Data Analytics Specialist  | Apr 2026 - Present \nNorth Group| Kerala \n"
    "• Analyse revenue across four branches, comparing staff performance and revenue per order to \nsupport business reviews. \n"
    "Finance & Accounts Executive | Jul 2025 - Mar 2026 \nSouth Travels | Kerala \n• Performed bank reconciliations and prepared cash reports. \n"
    "Finance Intern | Apr 2024 - Jun 2024 \nMetaCo | Bengaluru, India \n• Conducted market research. \n"
    "EDUCATION \nPost Graduate Diploma in Management (PGDM / MBA)| Jul 2023 - Jul 2025 \nBachelor of Commerce | Jun 2019 - Mar 2022 \n"
    "TECHNICAL SKILLS AND PROFESSIONAL DEVELOPMENT \nAnalysis: Advanced Excel, Power BI, SQL and Python. \n"
    "Certifications and courses: NISM Series V-A Mutual Fund Distributors Certification; Google Analytics Certification; CISA \nFoundation Course (Udemy). \n"
)


class RealLayoutTests(TestCase):
    def test_title_comes_from_summary(self):
        p = parser.parse_resume(KENZ_LIKE)
        self.assertEqual(p["primary_title"], "Entry-level Data Scientist")
        self.assertEqual(p["title_source"], "summary")
        self.assertEqual(p["latest_title"], "Data Science Trainee")
        self.assertEqual(p["titles"][0], "Entry-level Data Scientist")

    def test_title_falls_back_to_latest_role(self):
        p = parser.parse_resume(ADITH_LIKE)
        self.assertEqual(p["primary_title"], "Internal Audit & Data Analytics Specialist")
        self.assertEqual(p["title_source"], "experience")

    def test_pdf_damage_repaired(self):
        p = parser.parse_resume(KENZ_LIKE)
        self.assertIn("Data Science and Analytics Certification (HP Life)", p["certifications"])
        self.assertEqual(p["links"], ["linkedin.com/in/asha-r-nair"])
        self.assertIn("activities", p["sections_found"])
        self.assertIn("certifications", p["sections_found"])
        self.assertIn("profit margin", " ".join(p["experience"]))

    def test_inline_certifications_wrapped_line(self):
        p = parser.parse_resume(ADITH_LIKE)
        self.assertIn("CISA Foundation Course (Udemy)", p["certifications"])
        self.assertIn("Google Analytics Certification", p["certifications"])

    def test_month_technique(self):
        from datetime import date
        today = date(2026, 10, 5)
        s = parser.month_spans
        self.assertEqual(parser.months_worked(s(["Aug 2025 - Jan 2026"], today)), 6)       # Aug..Jan inclusive
        self.assertEqual(parser.months_worked(s(["Apr 2026 - Present"], today)), 7)        # Apr..Oct
        self.assertEqual(parser.months_worked(s(["Web Intern May 2023"], today)), 1)       # lone month
        self.assertEqual(parser.months_worked(s(["2021 - 2023"], today)), 36)              # years only
        self.assertEqual(parser.months_worked(s(["01/2024 - 06/2024"], today)), 6)         # numeric dates
        self.assertEqual(parser.months_worked(s(["Jan 2020 - Jun 2020", "Mar 2020 - Aug 2020"], today)), 8)  # overlap once
        self.assertEqual(parser.months_worked(s(["Jan 2027 - Dec 2027"], today)), 0)       # future ignored
        # the three Adith-style roles: 7 + 9 + 3 = 19 months = 1 yr 7 mo
        total = parser.months_worked(s(["Apr 2026 - Present", "Jul 2025 - Mar 2026", "Apr 2024 - Jun 2024"], today))
        self.assertEqual((total, parser.experience_label(total)), (19, "1 yr 7 mo"))

    def test_education_and_volunteering_not_counted(self):
        p = parser.parse_resume(KENZ_LIKE)
        self.assertEqual(p["experience_months"], 7)              # 6 (trainee) + 1 (intern); not 2021-2025 or NSS
        self.assertEqual(p["experience_source"], "calculated from dates")

    def test_stated_experience_wins(self):
        txt = KENZ_LIKE.replace("Entry-level Data Scientist with", "Data Scientist with 3 years of experience in Python, with")
        p = parser.parse_resume(txt)
        self.assertEqual(p["experience_months"], 36)
        self.assertEqual(p["experience_source"], "stated in resume")
        self.assertEqual(parser.stated_experience_months("8 months of hands-on experience"), 8)

    def test_entries_glue_wrapped_bullets(self):
        p = parser.parse_resume(KENZ_LIKE)
        e = parser.group_entries(p["experience"])
        self.assertEqual(len(e), 2)
        self.assertEqual(len(e[0]["bullets"]), 2)
        self.assertIn("92% accuracy and enabling", e[0]["bullets"][1])


class LatexTests(TestCase):
    def test_escape(self):
        self.assertEqual(latex.esc("R&D 50% #1 a_b {x}"), r"R\&D 50\% \#1 a\_b \{x\}")
        self.assertEqual(latex.esc("“quote” – dash •"), '"quote" - dash -')

    def test_generated_source_is_ats_friendly_and_valid(self):
        p = parser.parse_resume(KENZ_LIKE)
        src = latex.build_latex(p)
        self.assertIsNone(latex.validate_source(src))
        for bad in ("\\begin{tabular}", "\\includegraphics", "\\faIcon", "\\color"):
            self.assertNotIn(bad, src)
        self.assertIn("\\pdfgentounicode=1", src)
        self.assertIn("Data Science Trainee", src)
        self.assertIn("Aug 2025 - Jan 2026", src)
        self.assertIn("Price Prediction System", src)
        self.assertNotIn("Project-Link", src)

    def test_tailored_only_confirmed_skills(self):
        p = parser.parse_resume(RESUME)
        ev = analysis.evaluate(p, JOBS[1])
        src = latex.build_latex(p, ev, extra_skills=["AWS"], pursuing_certs=["AWS Certified Solutions Architect"])
        self.assertIn("AWS", src)
        self.assertIn("(in progress)", src)
        self.assertNotIn("Terraform", src)

    def test_validator_blocks_dangerous_latex(self):
        base = latex.build_latex(parser.parse_resume(KENZ_LIKE))
        self.assertIsNone(latex.validate_source(base))
        for evil in (r"\input{/etc/passwd}", r"\immediate\write18{ls}", r"\openin5=/etc/passwd", r"\usepackage{shellesc}",
                     r"\csname x\endcsname", r"\pdfshellescape", r"\write18{id}"):
            self.assertIsNotNone(latex.validate_source(base.replace(r"\begin{document}", evil + r"\begin{document}")), evil)
        self.assertIsNotNone(latex.validate_source("hello"))

    @unittest.skipUnless(latex.compiler_available(), "pdflatex not installed")
    def test_compiles_to_real_pdf_with_selectable_text(self):
        pdf, err = latex.compile_pdf(latex.build_latex(parser.parse_resume(KENZ_LIKE)))
        self.assertIsNone(err)
        self.assertTrue(pdf.startswith(b"%PDF"))
        from pypdf import PdfReader
        text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        self.assertIn("Entry-level Data Scientist", text)
        self.assertIn("Data Science Trainee", text)

    @unittest.skipUnless(latex.compiler_available(), "pdflatex not installed")
    def test_latex_errors_are_reported_not_raised(self):
        pdf, err = latex.compile_pdf("\\documentclass{article}\\begin{document}\\undefinedcommand\\end{document}")
        self.assertIsNone(pdf)
        self.assertIn("Undefined control sequence", err)

    @unittest.skipUnless(latex.compiler_available(), "pdflatex not installed")
    def test_shell_escape_is_off(self):
        evil = "\\documentclass{article}\\begin{document}\\immediate\\write18{touch /tmp/pwned_by_latex}\\end{document}"
        self.assertIsNotNone(latex.compile_pdf(evil)[1])
        import os
        self.assertFalse(os.path.exists("/tmp/pwned_by_latex"))


@override_settings(RESUME_SYNC_SEARCH=True)
class EditorFlowTests(TestCase):
    def setUp(self):
        self.client.post(reverse("upload"), {"resume": SimpleUploadedFile("k.txt", KENZ_LIKE.encode())})
        self.resume = Resume.objects.get()

    def test_create_edit_compile_download_and_cleanup(self):
        r = self.client.post(reverse("latex_new", args=[self.resume.pk]))
        gen = GeneratedResume.objects.get()
        self.assertRedirects(r, reverse("editor", args=[self.resume.pk, gen.pk]))
        page = self.client.get(r["Location"])
        self.assertContains(page, "\\documentclass")
        self.assertContains(page, "Compile PDF")

        edited = open(gen.file.path, encoding="utf-8").read().replace("Asha R Nair", "Asha Edited")
        url = reverse("editor_compile", args=[self.resume.pk, gen.pk])
        resp = self.client.post(url, {"source": edited})
        if latex.compiler_available():
            self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("Asha Edited", open(gen.file.path, encoding="utf-8").read())      # edit persisted

        bad = self.client.post(url, {"source": edited.replace(r"\begin{document}", r"\input{/etc/passwd}\begin{document}")})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("not allowed", bad.json()["error"])

        tex = self.client.get(reverse("editor_tex", args=[self.resume.pk, gen.pk]))
        self.assertIn(".tex", tex["Content-Disposition"])

        other = self.client_class()                                  # another visitor cannot open it
        self.assertEqual(other.get(reverse("editor", args=[self.resume.pk, gen.pk])).status_code, 404)

        import os
        path = gen.file.path
        Resume.objects.update(expires_at=dj_tz.now() - timedelta(seconds=1))
        purge_expired()
        self.assertFalse(os.path.exists(path))                       # the .tex is deleted with the resume


# ---------- tech hubs + official park sources ----------
from .services import hubs as hubs_mod


class HubTests(TestCase):
    def test_kerala_hubs_exist(self):
        for k in ("infopark", "technopark", "cyberpark"):
            self.assertIn(k, hubs_mod.HUBS)

    def test_location_matching(self):
        self.assertEqual(hubs_mod.matching_hubs("Kakkanad, Kochi", ["infopark", "technopark"]), ["infopark"])
        self.assertEqual(hubs_mod.matching_hubs("Cyberpark, Kozhikode (Calicut), Kerala", ["cyberpark"]), ["cyberpark"])
        self.assertEqual(hubs_mod.matching_hubs("Remote", ["infopark"]), [])
        self.assertEqual(hubs_mod.clean_selection(["infopark", "bogus", "infopark"]), ["infopark"])


class HubSearchTests(TestCase):
    def setUp(self):
        self.p = parser.parse_resume(RESUME)

    def test_official_park_jobs_are_merged_without_board_apis(self):
        kochi = dict(_job("Python Developer", "NewCo", "Python, Django, Docker. 3 years experience.", 1), location="Kochi, India")
        park = dict(_job("Python Developer", "InfoparkCo", "Python, Django, PostgreSQL.", 1), location="Infopark, Kochi, Kerala", source="Infopark")
        far = dict(_job("Backend Engineer", "FarCo", "Python, Flask.", 1), location="Berlin")
        with mock.patch.object(analysis, "fetch_all", return_value=([kochi, far], [], 2)), \
             mock.patch.object(analysis, "fetch_park_jobs", return_value=([park], [], 1)):
            results, stats = analysis.search_and_rank(self.p, hubs=["infopark"])
        titles = sorted((r["company"], r["title"]) for r in results)
        self.assertEqual(titles, [("InfoparkCo", "Python Developer"), ("NewCo", "Python Developer")])
        self.assertEqual(stats["board_providers"], [])
        self.assertEqual(stats["park_sources"], ["https://infopark.in/companies-job"])

    def test_industry_filter_matches_posting_or_company_metadata(self):
        fintech = dict(_job("Senior Python Payments Engineer", "PayCo", "Python payments.", 1), location="Kochi, India")
        tech = dict(_job("Senior Python Backend Engineer", "TechCo", "Python backend.", 1), location="Bangalore, India")
        with mock.patch.object(analysis, "fetch_all", return_value=([fintech, tech], [], 2)), \
             mock.patch.object(analysis, "fetch_park_jobs", return_value=([], [], 0)):
            results, stats = analysis.search_and_rank(self.p, industries=["fintech"])
        self.assertEqual([r["company"] for r in results], ["PayCo"])
        self.assertEqual(stats["industries"], ["fintech"])


# ---------- speed + registry tooling ----------
import tempfile
import time as _time
from io import StringIO
from pathlib import Path as _Path

import yaml as _yaml
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from .management.commands import validate_registry as vr
from .services import catalog as _catalog, jobs as _jobs

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "speed-tests"}}


@override_settings(CACHES=_LOCMEM)
class SpeedTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_registry_yaml_is_parsed_once_and_reloaded_on_edit(self):
        with tempfile.TemporaryDirectory() as d, override_settings(BASE_DIR=_Path(d)):
            f = _Path(d) / "companies.yaml"
            f.write_text("companies:\n  - {name: Acme, ats: lever, token: acme, industries: [energy]}\n", encoding="utf-8")
            with mock.patch.object(_catalog.yaml, "load", wraps=_catalog.yaml.load) as ld:
                for _ in range(50):
                    self.assertEqual(_catalog.enrich_job({"company": "Acme", "title": "Engineer", "description": ""})["company_industries"], ["energy"])
                self.assertEqual(ld.call_count, 1)              # 50 jobs, ONE parse
            f.write_text("companies:\n  - {name: Acme, ats: lever, token: acme, industries: [fintech]}\n", encoding="utf-8")
            import os
            os.utime(f, ns=(_time.time_ns() + 5_000_000_000,) * 2)
            self.assertEqual(_catalog.enrich_job({"company": "Acme", "title": "x", "description": ""})["company_industries"], ["fintech"])

    def test_dead_source_is_remembered_and_not_retried(self):
        c = {"name": "Dead", "ats": "greenhouse", "token": "dead"}
        with mock.patch.dict(_jobs.READERS, {"greenhouse": mock.Mock(side_effect=RuntimeError("404"))}) as _:
            r1 = _jobs.fetch_company(c)
            r2 = _jobs.fetch_company(c)
            self.assertEqual(r1, ([], "Dead: RuntimeError"))
            self.assertEqual(r2, ([], "Dead: RuntimeError"))
            self.assertEqual(_jobs.READERS["greenhouse"].call_count, 1)

    @override_settings(JOB_SEARCH_DEADLINE=1)
    def test_search_deadline_does_not_wait_for_the_slowest_site(self):
        def slow(c):
            if c["name"] == "Slow":
                _time.sleep(3)
            return [], None
        cs = [{"name": "Fast", "ats": "lever", "token": "f"}, {"name": "Slow", "ats": "lever", "token": "s"}]
        t = _time.time()
        with mock.patch.object(_jobs, "fetch_company", side_effect=slow):
            jobs_, errors, total = _jobs.fetch_all(cs)
        self.assertLess(_time.time() - t, 2.5)
        self.assertEqual((total, errors), (2, ["Slow: Timeout"]))

    def test_park_listing_detail_no_longer_crashes(self):
        j = {"detail": {"park_source": "infopark", "url": "https://infopark.in/x"}, "description": "", "title": "QA", "company": "C", "url": "https://infopark.in/x"}
        with mock.patch.object(_jobs, "_park_detail_text", return_value="Selenium and Python"):
            self.assertIn("Selenium", _jobs.fetch_detail(j)["description"])
        j2 = dict(j, description="", detail={"park_source": "infopark", "url": "https://infopark.in/y"}, url="https://infopark.in/y")
        with mock.patch.object(_jobs, "_park_detail_text", side_effect=RuntimeError("down")):
            self.assertEqual(_jobs.fetch_detail(j2)["description"], "")      # never raises


class RegistryToolTests(SimpleTestCase):
    def _project(self, d):
        (_Path(d) / "companies.yaml").write_text(
            "# keep me\ncompanies:\n  - {name: Old Live, ats: lever, token: oldlive}\n  - {name: Old Dead, ats: greenhouse, token: olddead}\n", encoding="utf-8")
        (_Path(d) / "companies_candidates.yaml").write_text(
            "companies:\n  - {name: Good Co, ats: greenhouse, token: goodco, regions: [africa]}\n"
            "  - {name: Fixable, ats: greenhouse, token: wrong}\n  - {name: Hopeless, ats: lever, token: nope}\n  - {name: Old Live, ats: lever, token: oldlive}\n", encoding="utf-8")

    def test_candidates_are_promoted_only_when_reachable(self):
        def fake_probe(e):
            ok = {("greenhouse", "goodco"): 3, ("lever", "fixable"): 2, ("lever", "oldlive"): 1}
            n = ok.get((e["ats"], e["token"]))
            return (n, f"https://x/{e['token']}", None) if n else (0, "", "HTTPStatusError")
        with tempfile.TemporaryDirectory() as d, override_settings(BASE_DIR=_Path(d), COMPANIES_FILE=_Path(d) / "companies.yaml"):
            self._project(d)
            out = StringIO()
            with mock.patch.object(vr, "probe", side_effect=fake_probe):
                call_command("validate_registry", candidates="companies_candidates.yaml", stdout=out)
            text = (_Path(d) / "companies.yaml").read_text(encoding="utf-8")
            self.assertIn("# keep me", text)                                   # comments preserved
            data = {c["name"]: c for c in _yaml.safe_load(text)["companies"]}
            self.assertEqual(data["Good Co"]["regions"], ["africa"])           # metadata carried over
            self.assertEqual((data["Fixable"]["ats"], data["Fixable"]["token"]), ("lever", "fixable"))   # wrong guess auto-corrected
            self.assertNotIn("Hopeless", data)
            self.assertEqual(len(data), 4)                                     # Old Live not duplicated
            self.assertIn("Hopeless", (_Path(d) / "companies_unreachable.txt").read_text(encoding="utf-8"))

    def test_prune_removes_only_dead_lines_and_keeps_backup(self):
        def fake_probe(e):
            return (5, "u", None) if e["token"] == "oldlive" else (0, "", "HTTPStatusError")
        with tempfile.TemporaryDirectory() as d, override_settings(BASE_DIR=_Path(d), COMPANIES_FILE=_Path(d) / "companies.yaml"):
            self._project(d)
            with mock.patch.object(vr, "probe", side_effect=fake_probe):
                call_command("validate_registry", live=True, prune=True, stdout=StringIO())
            text = (_Path(d) / "companies.yaml").read_text(encoding="utf-8")
            self.assertIn("Old Live", text); self.assertNotIn("Old Dead", text); self.assertIn("# keep me", text)
            self.assertTrue((_Path(d) / "companies.yaml.bak").exists())


@override_settings(CACHES=_LOCMEM, JOB_CACHE_SECONDS=100)
class StaleWhileRevalidateTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        _jobs._refreshing.clear()

    def _job(self, t):
        return {"title": t, "url": "https://x/" + t, "company": "A"}

    def test_fresh_cache_makes_no_network_call(self):
        c = {"name": "A", "ats": "lever", "token": "a"}
        reader = mock.Mock(return_value=iter([self._job("Dev")]))
        with mock.patch.dict(_jobs.READERS, {"lever": reader}):
            _jobs.fetch_company(c); _jobs.fetch_company(c); _jobs.fetch_company(c)
        self.assertEqual(reader.call_count, 1)

    def test_expired_cache_is_served_instantly_and_refreshed_in_background(self):
        from django.core.cache import cache
        c = {"name": "A", "ats": "lever", "token": "a"}
        cache.set(_jobs._source_key(c), {"jobs": [self._job("Old")], "at": _time.time() - 5000}, 9999)
        started = []
        def slow_reader(cc, client):
            started.append(1); _time.sleep(1.5); return iter([self._job("New")])
        with mock.patch.dict(_jobs.READERS, {"lever": slow_reader}):
            t = _time.time()
            got, err = _jobs.fetch_company(c)
            self.assertLess(_time.time() - t, 0.5)                 # did not wait for the slow site
            self.assertEqual([j["title"] for j in got], ["Old"])
            for _ in range(40):                                     # background refresh lands
                _time.sleep(0.1)
                if [j["title"] for j in cache.get(_jobs._source_key(c))["jobs"]] == ["New"]:
                    break
        self.assertEqual([j["title"] for j in _jobs.fetch_company(c)[0]], ["New"])

    def test_sources_without_job_data_are_rechecked_rarely(self):
        c = {"name": "E", "ats": "jsonld", "url": "https://e.example/careers"}
        reader = mock.Mock(return_value=iter([]))
        with mock.patch.dict(_jobs.READERS, {"jsonld": reader}):
            _jobs.fetch_company(c)
            with override_settings(JOB_CACHE_SECONDS=0):            # normal TTL passed, empty TTL (6 h) has not
                _jobs.fetch_company(c)
        self.assertEqual(reader.call_count, 1)


# ---------- park readers: structure-independent, paginated, with fallbacks ----------
import httpx as _httpx  # noqa: E402
from .services import park_sources as _ps  # noqa: E402

_ROWS = ('<table><tr><th>Date of Posting</th><th>Job Title</th><th>Company</th><th>Last Date</th><th></th></tr>{}</table>'
         '<a rel="next" href="?page={}">Next</a>')


def _row(i, title):
    return (f'<tr><td>0{i % 9 + 1}-Oct-2026</td><td>{title}</td><td>Co{i}</td><td>30-Oct-2026</td>'
            f'<td><a href="/companies/job-details/{i}">Details</a></td></tr>')


def _client(handler):
    return _httpx.Client(transport=_httpx.MockTransport(handler), follow_redirects=True)


class ParkReaderTests(TestCase):
    def test_table_with_pagination_keeps_design_jobs(self):
        def handler(req):
            page = req.url.params.get("page", "1")
            rows = {"1": _row(1, "UI/UX Designer") + _row(2, "Java Developer"),
                    "2": _row(3, "Graphic Designer") + _row(4, "Accountant"), "3": ""}[page]
            return _httpx.Response(200, text=_ROWS.format(rows, int(page) + 1), headers={"content-type": "text/html"})
        notes = []
        jobs, _ = _ps.read_listing("infopark", "https://infopark.in/companies/job-search", _client(handler), notes)
        self.assertEqual([j["title"] for j in jobs], ["UI/UX Designer", "Java Developer", "Graphic Designer", "Accountant"])
        self.assertEqual(jobs[0]["company"], "Co1")
        self.assertTrue(jobs[0]["posted"].startswith("2026-10-02"))        # posting date, NOT the closing date
        self.assertTrue(jobs[0]["url"].endswith("/companies/job-details/1"))
        self.assertIn("page(s)", " ".join(notes))

    def test_cards_json_ld_and_embedded_json(self):
        cards = ('<div class="x"><div><h4><a href="/job-details/9">Senior UX Designer</a></h4><p>Zyber</p><span>Posted on 02 Oct 2026</span></div>'
                 '<div><h4><a href="/job-details/10">Web Designer</a></h4><p>Beta</p></div></div><footer><a href="/careers">Careers</a></footer>')
        got = _ps.jobs_from_html(cards, "technopark", "https://technopark.org/job-search")
        self.assertEqual([j["title"] for j in got], ["Senior UX Designer", "Web Designer"])
        ld = '<script type="application/ld+json">{"@type":"JobPosting","title":"UI Designer","hiringOrganization":{"name":"Q"},"datePosted":"2026-10-01","url":"/j/1"}</script>'
        self.assertEqual(_ps.jobs_from_jsonld(ld, "cyberpark", "https://c.org/")[0]["company"], "Q")
        emb = '<script id="__NEXT_DATA__" type="application/json">{"props":{"jobs":[{"title":"Web Designer","company":"Z","url":"/j/1"}]}}</script>'
        self.assertEqual(_ps.jobs_from_embedded(emb, "cyberpark", "https://c.org/")[0]["title"], "Web Designer")

    def test_js_page_falls_back_to_wordpress_rest(self):
        def handler(req):
            u = str(req.url)
            if u.endswith("/careers/"):
                return _httpx.Response(200, text='<html><body><div id="root"></div></body></html>')
            if u.endswith("/wp-json/wp/v2/types"):
                return _httpx.Response(200, json={"job": {"name": "Jobs", "rest_base": "job"}, "page": {"name": "Pages", "rest_base": "pages"}})
            if "/wp-json/wp/v2/job" in u:
                return _httpx.Response(200, json=[{"title": {"rendered": "UI/UX Design Intern"}, "link": "https://www.cyberparkkerala.org/job/ui-ux/",
                                                   "date_gmt": "2026-10-04T08:00:00", "content": {"rendered": "<p>Figma, wireframes</p>"},
                                                   "acf": {"company_name": "Cyber Co"}}])
            return _httpx.Response(404)
        notes = []
        jobs, _ = _ps.read_listing("cyberpark", "https://www.cyberparkkerala.org/careers/", _client(handler), notes)
        self.assertEqual((jobs[0]["title"], jobs[0]["company"]), ("UI/UX Design Intern", "Cyber Co"))
        self.assertIn("Figma", jobs[0]["description"])
        self.assertTrue(any("JavaScript" in n for n in notes))

    def test_script_api_discovery(self):
        def handler(req):
            u = str(req.url)
            if u.endswith("/job-search"):
                return _httpx.Response(200, text='<html><body><div id="app"></div><script src="/static/app.js"></script></body></html>')
            if u.endswith("app.js"):
                return _httpx.Response(200, text='axios.get("/api/v1/jobs/list").then(r=>r)')
            if u.endswith("/wp-json/wp/v2/types"):
                return _httpx.Response(404)
            if u.endswith("/api/v1/jobs/list"):
                return _httpx.Response(200, json={"data": [{"id": 5, "designation": "UX Designer", "company": "Tech Co", "posted_on": "2026-10-03"}]})
            return _httpx.Response(404)
        jobs, _ = _ps.read_listing("technopark", "https://technopark.org/job-search", _client(handler), [])
        self.assertEqual(jobs[0]["title"], "UX Designer")

    def test_failure_is_reported_and_empty_is_not_cached_long(self):
        def handler(req):
            return _httpx.Response(403, text="blocked")
        notes = []
        jobs, _ = _ps.read_listing("infopark", "https://infopark.in/x", _client(handler), notes)
        self.assertEqual(jobs, [])
        self.assertIn("HTTP 403", notes[0])
        with mock.patch.object(_ps, "fetch_park_report", return_value=([], {"park": "infopark", "name": "Infopark", "jobs": 0, "ok": False, "notes": ["x"]})):
            from django.core.cache import cache
            cache.clear()
            _ps.fetch_park("infopark")
            self.assertEqual(cache.get("parkreport:infopark")["ok"], False)


class DesignTitleTests(TestCase):
    def test_design_roles_match_a_ui_ux_resume(self):
        titles = ["Versatile Software Engineer", "Software Developer", "UI/UX Design Intern"]
        for t in ["UI/UX Designer", "Senior UX Designer", "Product Designer (UI/UX)", "Graphic Designer", "Web Designer", "UI Developer"]:
            self.assertGreaterEqual(analysis.title_score(titles, t), 0.35, t)
        for t in ["Accountant", "Sales Manager"]:
            self.assertEqual(analysis.title_score(titles, t), 0.0)


class BrokenCertificateTests(TestCase):
    def test_hostname_mismatch_falls_back_once_and_is_reported(self):
        from .services.netutil import SafeClient

        def bad(req):
            raise _httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch")

        def good(req):
            return _httpx.Response(200, text=_ROWS.format(_row(1, "UI/UX Designer") + _row(2, "Web Designer"), 2).replace('rel="next"', ''))
        sc = SafeClient(5, {}, primary=_httpx.Client(transport=_httpx.MockTransport(bad)),
                        fallback_factory=lambda: _httpx.Client(transport=_httpx.MockTransport(good)))
        notes = []
        jobs, _ = _ps.read_listing("cyberpark", "https://www.cyberparkkerala.org/careers/", sc, notes)
        self.assertEqual([j["title"] for j in jobs], ["UI/UX Designer", "Web Designer"])
        self.assertEqual(sc.insecure_hosts, {"www.cyberparkkerala.org"})

    def test_fallback_can_be_disabled_and_other_errors_still_raise(self):
        from .services.netutil import SafeClient

        def bad(req):
            raise _httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] Hostname mismatch")
        with override_settings(PARK_INSECURE_FALLBACK=False):
            sc = SafeClient(5, {}, primary=_httpx.Client(transport=_httpx.MockTransport(bad)))
            with self.assertRaises(_httpx.ConnectError):
                sc.get("https://x.org/")

        def timeout(req):
            raise _httpx.ConnectTimeout("slow")
        sc = SafeClient(5, {}, primary=_httpx.Client(transport=_httpx.MockTransport(timeout)))
        with self.assertRaises(_httpx.ConnectTimeout):
            sc.get("https://x.org/")


class DefaultParksTests(TestCase):
    def test_every_search_includes_all_parks_without_selection(self):
        p = parser.parse_resume(RESUME)
        park = dict(_job("Python Developer", "ParkCo", "Python, Django, Docker. 3 years.", 1), location="Cyberpark, Kozhikode, Kerala", source="Cyberpark")
        seen = {}

        def fake(keys, progress=None):
            seen["keys"] = list(keys)
            return [park], [], len(keys)
        ats = [dict(_job("Python Developer", "FarCo", "Python, Django. 3 years.", 2), location="Austin, Texas")]
        with mock.patch.object(analysis, "fetch_all", return_value=(ats, [], 1)), mock.patch.object(analysis, "fetch_park_jobs", fake):
            results, stats = analysis.search_and_rank(p)
        self.assertEqual(set(seen["keys"]), {"infopark", "technopark", "cyberpark"})
        self.assertEqual({r["company"] for r in results}, {"ParkCo", "FarCo"})      # ATS jobs are NOT location-restricted
        self.assertEqual(stats["park_jobs"], 1)

    def test_resume_page_has_no_optional_selectors(self):
        self.client.post(reverse("upload"), {"resume": SimpleUploadedFile("j.txt", RESUME.encode())})
        r = self.client.get(reverse("detail", args=[Resume.objects.get().pk]))
        self.assertNotContains(r, "(optional)")
        self.assertContains(r, "Infopark, Technopark and Cyberpark")



class PrivacyAndSnapshotTests(TestCase):
    def setUp(self):
        self.client.post(reverse("upload"), {"resume": SimpleUploadedFile("cv.txt", RESUME.encode())})
        self.resume = Resume.objects.get()

    def test_upload_is_not_stored_on_disk(self):
        import os
        from django.conf import settings
        folder = settings.MEDIA_ROOT / "resumes"
        before = set(os.listdir(folder)) if folder.exists() else set()
        self.client.post(reverse("upload"), {"resume": SimpleUploadedFile("cv2.txt", RESUME.encode())})
        after = set(os.listdir(folder)) if folder.exists() else set()
        self.assertEqual(after - before, set())

    def test_starts_with_short_idle_deadline_and_hard_cap(self):
        left = (self.resume.expires_at - dj_tz.now()).total_seconds()
        self.assertTrue(150 <= left <= 180)
        self.assertTrue(3590 <= self.resume.seconds_left <= 3600)

    def test_ping_keeps_alive_and_never_passes_hard_cap(self):
        Resume.objects.update(expires_at=dj_tz.now() + timedelta(seconds=20))
        self.assertEqual(self.client.post(reverse("ping", args=[self.resume.pk])).status_code, 200)
        self.resume.refresh_from_db()
        self.assertTrue((self.resume.expires_at - dj_tz.now()).total_seconds() > 150)
        self.assertLessEqual(self.resume.expires_at, self.resume.hard_deadline)

    def test_no_heartbeat_means_deleted(self):
        Resume.objects.update(expires_at=dj_tz.now() - timedelta(seconds=1))
        self.assertEqual(purge_expired(), 1)
        self.assertEqual(Resume.objects.count(), 0)

    def test_expired_resume_cannot_be_revived_by_late_ping(self):
        Resume.objects.update(expires_at=dj_tz.now() - timedelta(seconds=1))
        self.assertEqual(self.client.post(reverse("ping", args=[self.resume.pk])).status_code, 404)
        self.assertEqual(Resume.objects.count(), 0)

    def test_leave_beacon_starts_short_countdown(self):
        self.client.post(reverse("leave", args=[self.resume.pk]))
        self.resume.refresh_from_db()
        self.assertTrue((self.resume.expires_at - dj_tz.now()).total_seconds() <= 46)
        self.client.get(reverse("detail", args=[self.resume.pk]))          # owner navigates on -> restored
        self.resume.refresh_from_db()
        self.assertTrue((self.resume.expires_at - dj_tz.now()).total_seconds() > 150)

    def test_other_browser_cannot_ping_leave_or_read(self):
        other = self.client_class()
        self.assertEqual(other.post(reverse("ping", args=[self.resume.pk])).status_code, 404)
        other.post(reverse("leave", args=[self.resume.pk]))
        self.resume.refresh_from_db()
        self.assertTrue((self.resume.expires_at - dj_tz.now()).total_seconds() > 100)     # untouched by the stranger
        self.assertEqual(other.get(reverse("detail", args=[self.resume.pk])).status_code, 404)
        self.assertEqual(other.post(reverse("delete", args=[self.resume.pk])).status_code, 404)

    def test_pages_are_not_cacheable(self):
        r = self.client.get(reverse("detail", args=[self.resume.pk]))
        self.assertIn("no-store", r["Cache-Control"])

    def test_snapshot_mode_makes_no_network_calls(self):
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        from matcher.services import jobs as jobs_mod, park_sources, snapshot
        job = {"company": "SnapCo", "title": "Python Developer", "url": "https://x.test/1", "location": "Kochi",
               "posted": "2026-10-01T00:00:00+00:00", "description": "Python Django"}
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "s.json.gz"
            snapshot.write_snapshot([job], ["Dead: Timeout"], 5, {"infopark": {"jobs": [dict(job, title="Infopark Dev")], "report": {"ok": True, "jobs": 1, "notes": [], "name": "Infopark", "park": "infopark"}}}, f)
            with override_settings(JOB_SOURCE="snapshot", JOB_SNAPSHOT_FILE=f):
                snapshot._mem["mtime"] = None
                with mock.patch("httpx.Client", side_effect=AssertionError("network used")), \
                     mock.patch("httpx.get", side_effect=AssertionError("network used")):
                    jobs, errors, total = jobs_mod.fetch_all()
                    self.assertEqual((len(jobs), errors, total), (1, ["Dead: Timeout"], 5))
                    pj, rep, _ = park_sources.fetch_park("infopark")
                    self.assertEqual(pj[0]["title"], "Infopark Dev")
                    jobs[0]["x"] = 1                                       # callers mutate: must not corrupt the cache
                    self.assertNotIn("x", snapshot.load()["jobs"][0])
            snapshot._mem["mtime"] = None


class SnapshotCommandTests(SimpleTestCase):
    JOB = {"company": "C", "title": "Dev", "url": "https://x.test/1", "location": "Kochi", "posted": "2026-10-01T00:00:00+00:00",
           "description": "", "detail": {"ats": "greenhouse", "token": "t", "id": 1}}

    def _run_update(self, f, jobs_returned, **kw):
        from matcher.services import park_sources
        def fill(todo, workers=24):
            for j in todo:
                j["description"] = "full text"
        with override_settings(JOB_SNAPSHOT_FILE=f), \
             mock.patch("matcher.services.jobs.fetch_all", return_value=(jobs_returned, [], 3)), \
             mock.patch("matcher.services.jobs.fetch_details", side_effect=fill), \
             mock.patch.object(park_sources, "fetch_park_report", return_value=([], {"ok": False, "jobs": 0, "notes": [], "name": "P", "park": "p"})):
            call_command("update_snapshot", stdout=StringIO(), **kw)

    def test_export_fills_descriptions_and_refuses_to_shrink(self):
        from matcher.services import snapshot
        with tempfile.TemporaryDirectory() as d:
            f = _Path(d) / "s.json.gz"
            self._run_update(f, [dict(self.JOB) for _ in range(10)])
            snapshot._mem["mtime"] = None
            with override_settings(JOB_SNAPSHOT_FILE=f):
                data = snapshot.load()
            self.assertEqual(len(data["jobs"]), 10)
            self.assertEqual(data["jobs"][0]["description"], "full text")
            self.assertNotIn("detail", data["jobs"][0])
            from django.core.management.base import CommandError
            with self.assertRaises(CommandError):                       # a broken run must not wipe a good snapshot
                self._run_update(f, [dict(self.JOB)])
            snapshot._mem["mtime"] = None
            with override_settings(JOB_SNAPSHOT_FILE=f):
                self.assertEqual(len(snapshot.load()["jobs"]), 10)
            snapshot._mem["mtime"] = None

    def test_pull_validates_before_replacing(self):
        import gzip, json
        from django.core.management.base import CommandError
        from matcher.services import snapshot
        good = gzip.compress(json.dumps({"jobs": [{"title": "a"}]}).encode())
        class R:
            def __init__(s, c): s.content = c
            def raise_for_status(s): pass
        with tempfile.TemporaryDirectory() as d:
            f = _Path(d) / "s.json.gz"
            with override_settings(JOB_SNAPSHOT_FILE=f):
                with mock.patch("httpx.get", return_value=R(good)):
                    call_command("pull_snapshot", url="https://x.test/s", stdout=StringIO())
                self.assertEqual(f.read_bytes(), good)
                with mock.patch("httpx.get", return_value=R(b"<html>rate limited</html>")):
                    with self.assertRaises(CommandError):
                        call_command("pull_snapshot", url="https://x.test/s", stdout=StringIO())
                self.assertEqual(f.read_bytes(), good)                  # old file kept
            snapshot._mem["mtime"] = None
