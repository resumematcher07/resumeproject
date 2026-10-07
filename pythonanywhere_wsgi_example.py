# Paste this into the PythonAnywhere WSGI file (Web tab -> "WSGI configuration file"), replacing everything.
# Replace YOURUSER and the SECRET value.
import os, sys

path = '/home/YOURUSER/resume_job_matcher'
if path not in sys.path:
    sys.path.insert(0, path)

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
os.environ['DJANGO_SECRET_KEY'] = 'PASTE-A-LONG-RANDOM-STRING'          # python -c "import secrets;print(secrets.token_urlsafe(50))"
os.environ['DJANGO_DEBUG'] = '0'
os.environ['DJANGO_ALLOWED_HOSTS'] = 'YOURUSER.pythonanywhere.com'
os.environ['DJANGO_CSRF_ORIGINS'] = 'https://YOURUSER.pythonanywhere.com'

os.environ['JOB_SOURCE'] = 'snapshot'            # read GitHub-built data, never scrape on PythonAnywhere
os.environ['SNAPSHOT_URL'] = 'https://raw.githubusercontent.com/YOURGITHUB/YOURREPO/data/jobs_snapshot.json.gz'
os.environ['RESUME_IDLE_SECONDS'] = '180'        # delete 3 min after the last heartbeat (tab closed / force-stopped)
os.environ['RESUME_TTL_SECONDS'] = '3600'        # absolute maximum life: 1 hour
os.environ['JOB_START_WARMER'] = '0'

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
