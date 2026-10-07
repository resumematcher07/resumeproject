# Deploy: scraping on GitHub, everything else on PythonAnywhere (free)

## How the pieces connect

```
 GitHub Actions (every 6 h, free, unrestricted internet)
   python manage.py update_snapshot      <- the scraping code runs HERE
        |  writes jobs_snapshot.json.gz (public jobs only, NO resumes)
        v
 GitHub branch "data"  --->  raw.githubusercontent.com/<you>/<repo>/data/jobs_snapshot.json.gz
                                          |
                                          |  python manage.py pull_snapshot   (daily task)
                                          v
 PythonAnywhere free:  Django site  (JOB_SOURCE=snapshot)
   upload resume -> parse in memory -> match against snapshot -> results
   NO outgoing scraping, so the free-plan whitelist is not a problem
```

`github.com` and `githubusercontent.com` are on PythonAnywhere's free-account allow-list, so the daily download works.

---

## PART 1 - GitHub (scraper + code)

1. Create a **public** repository on github.com (e.g. `resume-job-matcher`). Public is needed so PythonAnywhere can download the snapshot without a token. The snapshot only contains public job postings; your code contains no secrets.
2. On your PC, inside the project folder:

```bash
git init -b main
git add .
git status            # CHECK: db.sqlite3, media/, .cache/ must NOT be listed (.gitignore blocks them)
git commit -m "first version"
git remote add origin https://github.com/<YOUR_GITHUB>/resume-job-matcher.git
git push -u origin main
```

3. GitHub repo -> **Settings -> Actions -> General -> Workflow permissions -> "Read and write permissions" -> Save**.
4. GitHub repo -> **Actions** tab -> "Update job snapshot" -> **Run workflow**. Takes roughly 5-30 minutes the first time.
5. When it is green, open `https://raw.githubusercontent.com/<YOUR_GITHUB>/resume-job-matcher/data/jobs_snapshot.json.gz` - it should download a file. **This URL is what PythonAnywhere uses.**
6. Open the run log, step "Scrape all sources": it prints jobs per source and per IT-park. If a park shows `0 jobs`, see Troubleshooting.

After this it re-runs by itself every 6 hours. (GitHub pauses scheduled workflows in a repo with no activity for 60 days. If that happens, click "Enable workflow" or press "Run workflow" once.)

---

## PART 2 - PythonAnywhere (free)

Replace `YOURUSER` with your PythonAnywhere username everywhere.

### 2.1 Get the code
Dashboard -> **Consoles -> Bash**:

```bash
git clone https://github.com/<YOUR_GITHUB>/resume-job-matcher.git resume_job_matcher
cd resume_job_matcher
mkvirtualenv rm --python=python3.12
pip install -r requirements.txt          # NOT requirements-scraper.txt (no Playwright here)
python manage.py migrate
python manage.py collectstatic --noinput
```

### 2.2 Download the first snapshot

```bash
export JOB_SOURCE=snapshot
export SNAPSHOT_URL=https://raw.githubusercontent.com/<YOUR_GITHUB>/resume-job-matcher/data/jobs_snapshot.json.gz
python manage.py pull_snapshot
```
Expected: `installed snapshot with NNNN jobs`.

### 2.3 Create the web app
**Web tab -> Add a new web app -> Manual configuration -> Python 3.12**. Then on the Web tab:

| Setting | Value |
|---|---|
| Source code | `/home/YOURUSER/resume_job_matcher` |
| Working directory | `/home/YOURUSER/resume_job_matcher` |
| Virtualenv | `/home/YOURUSER/.virtualenvs/rm` |
| Static files | URL `/static/` -> Directory `/home/YOURUSER/resume_job_matcher/staticfiles` |
| **Force HTTPS** | Enabled |

**Do NOT add a static mapping for `/media/`.** Anything under media is private and must only be reachable through Django's owner check.

### 2.4 WSGI file
Web tab -> click the **WSGI configuration file** link -> delete everything -> paste the contents of `pythonanywhere_wsgi_example.py`, then edit `YOURUSER`, `YOURGITHUB`, `YOURREPO` and the secret key (generate with `python -c "import secrets;print(secrets.token_urlsafe(50))"`). Save.

### 2.5 Reload
Web tab -> green **Reload** button -> open `https://YOURUSER.pythonanywhere.com`.

### 2.6 Daily scheduled task (free plan allows one)
**Tasks tab -> Scheduled -> pick a time -> paste as ONE line:**

```bash
cd ~/resume_job_matcher && JOB_SOURCE=snapshot SNAPSHOT_URL=https://raw.githubusercontent.com/<YOUR_GITHUB>/resume-job-matcher/data/jobs_snapshot.json.gz ~/.virtualenvs/rm/bin/python manage.py pull_snapshot; ~/.virtualenvs/rm/bin/python manage.py purge_expired; ~/.virtualenvs/rm/bin/python manage.py clearsessions
```
It (1) refreshes the job data, (2) deletes any expired resumes, (3) cleans old sessions. A failed download keeps the previous snapshot.

### 2.7 Keep the free app alive
Free web apps stop after 3 months unless you press **"Run until 3 months from today"** on the Web tab. PythonAnywhere emails a reminder. Your database and files are not deleted by this, only the app is paused.

---

## PART 3 - How the resume privacy rules work (and how to test them)

| Your requirement | What the code does |
|---|---|
| Only the uploader can see their resume | Every resume is tied to a random session cookie. Every page, search, download and delete filters on that session. Anyone else, even with the exact link, gets **404**. Links use unguessable UUIDs. |
| Leave / close tab / force stop => delete | The open page sends a heartbeat every 30 s. If heartbeats stop for **3 minutes** (`RESUME_IDLE_SECONDS`) the resume, parsed data, results and generated files are deleted. Closing the tab also sends a "leaving" signal that shortens this to **45 s**. A late heartbeat can never revive an expired resume. |
| Hard limit | Even with the page open, a resume is deleted **1 hour** after upload (`RESUME_TTL_SECONDS`). |
| Original file | **No longer written to disk at all.** It is read in memory; only extracted text + parsed fields are kept until deletion. |
| Back button | All pages are sent with `Cache-Control: no-store`, so Back cannot show a deleted resume. |
| Browser closed | The session cookie is a browser-session cookie and disappears with the browser. |

**Test it (2 minutes):**
1. Upload a resume. Open the result URL in a private/incognito window -> must show 404.
2. Close the tab. Wait ~1 minute, open the site again -> your old resume is gone.
3. Upload again, leave the tab open, kill the browser from the task manager. Wait 4 minutes, open the site -> gone.
4. Click "Delete now" -> gone immediately.

### Honest limits (free plan)
- **Deletion happens when the site next gets a request, or when the daily task runs.** Free PythonAnywhere has no always-on process, so if nobody visits after a force-stop, the expired rows stay in `db.sqlite3` until the next visit/daily task. They are already unreachable by anyone (every page purges first), but physically they are still in the database until then. A paid account's hourly task would close this gap. To minimise exposure the original file is never stored.
- **Phone locked / tab in background for more than ~3 minutes** can stop the heartbeat, which deletes the resume. That is the intended behaviour for "leaves the site", but raise `RESUME_IDLE_SECONDS` (e.g. 300) if it is too strict.
- Free accounts have a small daily CPU allowance. Searching reads one compact file from disk (cached in memory) so it is cheap, but heavy traffic can slow the site for the rest of the day.
- Free accounts can't send resumes anywhere else and have no SSH-level secrets storage; keep `DJANGO_SECRET_KEY` only in the WSGI file (never in GitHub).

---

## PART 4 - Updating later without losing data

```bash
cd ~/resume_job_matcher && git pull
workon rm && pip install -r requirements.txt && python manage.py migrate && python manage.py collectstatic --noinput
```
then **Reload** on the Web tab. `db.sqlite3` and `media/` are in `.gitignore`, so `git pull` never overwrites them. To add companies, edit `companies.yaml`, push, and run the workflow once; the new companies appear in the next snapshot.

Backup of the database (optional): Files tab -> `resume_job_matcher/db.sqlite3` -> download. It only holds temporary resumes and sessions by design.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Results are empty / "Job snapshot not found" | Run `pull_snapshot` (step 2.2) and check `JOB_SOURCE=snapshot` in the WSGI file. |
| `pull_snapshot`: Download failed | Check the URL opens in a browser; repo must be public; the `data` branch exists (run the workflow once). |
| Workflow fails: "refusing to overwrite" | Too few jobs were scraped this run (network hiccup). Re-run; the old snapshot stays online. |
| A park (Infopark/Technopark/Cyberpark) shows 0 jobs | Open the workflow log. Some sites block GitHub's datacenter IPs. Try `python manage.py diagnose_parks --render` on your own PC to confirm the parser works; if the site blocks GitHub, run `update_snapshot` on your PC and upload the file (see below). |
| `DisallowedHost` | `DJANGO_ALLOWED_HOSTS` in the WSGI file must equal `YOURUSER.pythonanywhere.com`. |
| CSRF error on upload | `DJANGO_CSRF_ORIGINS` must be `https://YOURUSER.pythonanywhere.com`. |
| Site shows old code | Web tab -> Reload after `git pull`. |

**Fallback if GitHub is blocked by a source:** run `python manage.py update_snapshot` on your PC (needs `pip install -r requirements-scraper.txt && playwright install chromium`), then upload `data/jobs_snapshot.json.gz` through PythonAnywhere's **Files** tab into `resume_job_matcher/data/`.

## Settings reference (WSGI environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `JOB_SOURCE` | `live` | `snapshot` on PythonAnywhere free |
| `SNAPSHOT_URL` | - | raw GitHub URL of the snapshot |
| `RESUME_IDLE_SECONDS` | 180 | delete this long after the last heartbeat (0 = off) |
| `RESUME_LEAVE_SECONDS` | 45 | delete this long after a tab-close signal |
| `RESUME_TTL_SECONDS` | 3600 | absolute maximum life of a resume |
