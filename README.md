# ResumeMatch (Django)

Upload a resume -> every section is parsed -> company career pages are searched (newest postings first) ->
skill/experience/certificate gaps are shown -> a tailored resume (.docx) can be downloaded ->
**everything is auto-deleted 1 hour after upload** (or instantly via "Delete now").

## Run locally
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py runserver
    python manage.py test matcher

## How job search works
`companies.yaml` lists the companies. Greenhouse, Lever, Ashby and Workable expose public JSON feeds for their career
boards; any other careers page that embeds schema.org `JobPosting` data works with `ats: jsonld`.
Add companies by editing that file (token = slug in the company's board URL). Unreachable/wrong ones are skipped and reported.

## Tech hubs + official park sources
Every search automatically includes the official **Infopark, Technopark and Cyberpark** listings, all industries and all job functions - there is nothing to tick on the resume page.
For these Kerala parks, selection is no longer just a city keyword: the search merges the employer ATS registry with the
park's own official job listings. Employer jobs still use location matching as a fallback, so a posting that only says
"Kochi" can match Infopark.

No Adzuna, Jooble or JSearch API key is required. The core search uses public Greenhouse, Lever, Ashby, Workable and
JSON-LD career feeds plus official park listings.

### If a park shows 0 jobs (UI/UX, design or any other role missing)
Park pages differ in markup and some load their list with JavaScript, so the reader tries several methods in order
(WordPress `wp-json`, JSON-LD, JSON embedded in the page, tables/cards found by structure, API URLs found in the page's
scripts, and optionally a real headless browser). It follows pagination and **never filters by title**, so design roles are
not dropped at read time. The results page now lists, per park, how many openings were read and how.

    python manage.py diagnose_parks                 # what each method found, or the exact reason (HTTP 403, JavaScript page, timeout...)
    python manage.py diagnose_parks --save diag     # also keep the downloaded HTML in ./diag/
    python manage.py diagnose_parks --html page.html --park infopark   # test the parser on a page saved from your browser

If it says the page is JavaScript-rendered or blocked, the headless-browser fallback is on by default (`PARK_RENDER=1`) and runs automatically once the browser is installed
(your PC, not PythonAnywhere free):

    pip install -r requirements.txt && playwright install chromium
    python manage.py diagnose_parks --render

**Broken HTTPS certificates:** some park sites (e.g. cyberparkkerala.org) serve a certificate for a different host name, so strict
verification can never succeed. For these public job listings only, a certificate error triggers one retry without verification
(no cookies or logins are sent; the results page says so). Disable with `PARK_INSECURE_FALLBACK=0`.

PythonAnywhere **free** accounts can only reach whitelisted sites, so park/company pages outside that list always time out there.

Role matching also understands role families, so a "UI/UX Design Intern" resume matches UI/UX Designer, Product Designer,
Graphic Designer and Web Designer openings.

### Current Kerala figures used by the project
- **Infopark Kochi:** 572+ companies, 65,900+ professionals, 9.2 million sq ft built-up space (current official Infopark page).
- **Technopark Thiruvananthapuram:** 540 IT/IT-enabled-services companies and about 84,000 direct jobs (2025–26 figures reported from official data).
- **Cyberpark Kozhikode:** 100% occupancy, about 2,200 direct employment and ₹121 crore exports for FY 2023–24 (official Cyberpark page).

The figures are informational metadata; live job counts are fetched from the official park pages at search time and can change during the day.

## Deploy on PythonAnywhere
> **Use `DEPLOY.md`** - the current guide (scraping on GitHub Actions, site on PythonAnywhere free). The older notes below are kept for reference.

1. Open a Bash console:
       git clone <your repo> resume_job_matcher   # or upload the folder
       cd resume_job_matcher
       mkvirtualenv rm --python=python3.12 && pip install -r requirements.txt
       python manage.py migrate && python manage.py collectstatic --noinput
2. **Web tab -> Add a new web app -> Manual configuration** (same Python version). Set:
   - Virtualenv: `/home/<user>/.virtualenvs/rm`
   - Source/working directory: `/home/<user>/resume_job_matcher`
   - Static files: URL `/static/` -> `/home/<user>/resume_job_matcher/staticfiles`
     (do **not** map `/media/`; uploaded resumes must stay private)
3. WSGI file:
       import os, sys
       path = '/home/<user>/resume_job_matcher'
       if path not in sys.path: sys.path.append(path)
       os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
       os.environ['DJANGO_SECRET_KEY'] = '<long random string>'
       os.environ['DJANGO_DEBUG'] = '0'
       os.environ['DJANGO_ALLOWED_HOSTS'] = '<user>.pythonanywhere.com'
       os.environ['DJANGO_CSRF_ORIGINS'] = 'https://<user>.pythonanywhere.com'
       from django.core.wsgi import get_wsgi_application
       application = get_wsgi_application()
4. Reload the web app.
5. **Tasks tab**: add a scheduled task `cd ~/resume_job_matcher && ~/.virtualenvs/rm/bin/python manage.py purge_expired`
   (hourly on paid plans, daily on free). Expired resumes are *also* purged on every request and by a background thread,
   so the 1-hour rule holds even between scheduled runs while the site is being used.

### PythonAnywhere notes
- Free accounts can only reach whitelisted sites. If Greenhouse/Lever/Ashby aren't reachable, searches will show "unreachable"; a paid plan removes that limit.
- A search fetches many sites; results are cached 10 min (`JOB_CACHE_SECONDS`).
- Only text-based PDFs/DOCX/TXT are supported (no OCR for scanned images).

## Privacy / honesty design
- Files are stored under random names, are never served publicly, and are tied to the uploader's browser session.
- The tailored resume only contains facts from the original plus skills the candidate explicitly ticks as "I genuinely have this".
  Missing skills go to a learning plan; missing certificates are listed with the issuer link (or "in progress" if the candidate ticks it).

## Speed
- Companies are fetched in parallel (24 at once, 7 s timeout each) and cached for 30 minutes in a shared on-disk cache.
- Only job titles are downloaded first; full descriptions are fetched just for the newest title-relevant postings (`JOB_DETAIL_LIMIT`).
- The search runs in the background with a live progress bar; the first search is slowest, repeats are near-instant.
- To go faster still, keep `companies.yaml` to the companies you care about.

## Experience calculation
Only the Experience section is used (education, projects and volunteering are ignored).
1. If the resume states it ("3 years of experience", "8 months of experience"), that number is used.
2. Otherwise each role is counted from its first month to its last month (or the current month for "Present"),
   inclusive - `Aug 2025 - Jan 2026` = 6 months, a lone `May 2023` = 1 month - and overlapping roles count once.
Set `EXPERIENCE_METHOD=span` to count from the first job's start to the latest end instead (gaps included).

## ATS-friendly LaTeX resume
"ATS resume (LaTeX)" (on the resume page, or per job in the results) generates a single-column, plain-text-friendly
`.tex` (standard headings, selectable text, no tables/images/icons). The editor lets you change the LaTeX and press
**Compile PDF** (Ctrl+Enter) for a live preview; download the `.tex` or the PDF. PDFs are generated on the fly and never stored;
the `.tex` is deleted with the resume after 1 hour.
- Needs `pdflatex` on the server (PythonAnywhere provides it). Without it the editor still works and you can download the `.tex`.
- Compilation is sandboxed: shell-escape off, file reading/writing restricted, dangerous commands and unknown packages rejected, 30 s limit.

## Global company registry and IT-park search

The project now includes a file-backed global career registry:

- `companies.yaml` — global employers and ATS/career-page sources. Each company can carry `regions`, `industries`, and `job_functions` metadata.
- `it_parks.yaml` — major technology/business hubs worldwide, loaded dynamically by `matcher/services/hubs.py`.
- `job_functions.yaml` — Engineering, Data/AI, UI/UX & Design, Product, Business, Finance, Sales, Marketing, Operations, HR, Legal, Customer Support, Research, Healthcare and Executive categories.
- `industries.yaml` — technology, AI/ML, fintech, financial services, ecommerce, cybersecurity, telecom, semiconductor, automotive, aerospace, healthcare, pharma/biotech, energy, travel, logistics, gaming, education and other sectors.
- `matcher/services/catalog.py` — enriches each job with company metadata and lightweight job-function/industry classification. The resume search UI can filter by both job function and industry.

### Adding a company

Use one of the supported source types:

```yaml
- name: Example Company
  ats: greenhouse
  token: example-company
  regions: [europe]
  industries: [technology]
  job_functions: [engineering, data_ai, product, finance, business]
```

For a career page that exposes `JobPosting` JSON-LD:

```yaml
- name: Example Company
  ats: jsonld
  url: https://example.com/careers/
  regions: [europe]
  industries: [technology]
  job_functions: [engineering, finance, business]
```

Validate the registry with:

```bash
python manage.py validate_registry
```

ATS tokens and career URLs can change. A source that becomes unreachable is reported as an error during a search and does not stop other companies from being searched.

## Speed fixes (what changed and why)
- **YAML registry loaded once.** `catalog.py` used to re-parse `companies.yaml`, `job_functions.yaml` and `industries.yaml` for
  *every job* (about 125 ms each, so 20,000 jobs = ~40 min). They are now cached in memory and reloaded automatically only when
  the file changes on disk (and parsed with libyaml's C loader when available). Same results, ~600x faster classification.
- **Dead sources are remembered** (`JOB_ERROR_CACHE_SECONDS`, default 1 h) instead of being retried and waited on in every search.
- **More parallelism + a hard deadline**: `JOB_FETCH_WORKERS` (100) fetches at once; `JOB_SEARCH_DEADLINE` (30 s) stops waiting for
  slow sites, which are listed as "Timeout" under unreachable. Connect timeout is 4 s, per-request `JOB_FETCH_TIMEOUT` is 6 s.
- **Infopark crash fixed**: official park listings have no ATS id, so fetching their details raised `KeyError('ats')` and could abort
  the whole search. They now read the park's detail page (never raising).

All three knobs can be set with environment variables of the same name.

## Adding more companies that really work
`companies_candidates.yaml` holds ~410 extra companies (engineering, energy, construction-tech, fintech/finance, education/business
courses; Kerala, India, Middle East, Africa, Asia, Australia, Europe, North & South America). Their ATS/token values are guesses until
checked, so they are NOT loaded by searches. Verify and promote them on a machine with internet access:

    python manage.py validate_registry --candidates     # appends only the sources that respond to companies.yaml
    python manage.py validate_registry --live           # report dead sources already in companies.yaml
    python manage.py validate_registry --live --prune   # ...and remove them (companies.yaml.bak is kept)

Wrong ATS/token guesses are auto-corrected by trying Greenhouse, Lever, Ashby and Workable with name-based slugs. Afterwards open
`registry_report.txt` (sample job link per company - make sure the board belongs to the right company) and
`companies_unreachable.txt` (companies that use Workday/Taleo/custom portals; add those as `ats: jsonld` if their page exposes JobPosting data).

## Why searches still felt slow, and the fixes (v3)
- **Cache was being emptied (root cause).** Django's file cache defaults to 300 entries and then deletes a third of them. With 200+ sources
  plus job details and error markers the project exceeded that constantly, so freshly downloaded career data was thrown away and every
  search re-downloaded it. `CACHES` now sets `MAX_ENTRIES` to 50000.
- **Stale-while-revalidate.** An expired source list is returned instantly and refreshed in the background, so a search never waits for a
  site it has seen before. Sites that expose no job data (many `jsonld` pages are JavaScript-rendered) are re-checked only every 6 h
  (`JOB_EMPTY_CACHE_SECONDS`).
- **Background warmer.** While the server runs, a thread refreshes every source (`JOB_START_WARMER=1`, every `JOB_WARM_INTERVAL` = 25 min),
  so by the time you search the data is already cached. Where threads are not allowed or you prefer a separate process:
  `python manage.py warm_cache --loop` (or run `python manage.py warm_cache` as a scheduled task).
- **Find the slow sources on your own network:** `python manage.py source_timings` prints every source slowest-first;
  `python manage.py source_timings --quarantine 8` makes searches skip sources slower than 8 s or failing for 6 h. Remove chronic offenders
  from `companies.yaml`.
- Measured with 200 simulated sources (random 0.2-12 s latencies, 15 % dead): cold search ~12-25 s (first ever), warm search 0.04 s,
  search right after cache expiry 0.2 s.
"# resumeproject" 
"# resumeproject" 
