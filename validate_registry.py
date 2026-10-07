"""Offline validation for the file-backed global job registry.

Run: python validate_registry.py
This does not contact career sites.
"""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
ALLOWED_ATS = {"greenhouse", "lever", "ashby", "workable", "jsonld"}

companies = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))['companies']
functions = yaml.safe_load((ROOT / "job_functions.yaml").read_text(encoding="utf-8"))['job_functions']
parks = yaml.safe_load((ROOT / "it_parks.yaml").read_text(encoding="utf-8"))['it_parks']
industries = yaml.safe_load((ROOT / "industries.yaml").read_text(encoding="utf-8"))['industries']

names = [c['name'].casefold() for c in companies]
errors = []
for c in companies:
    if c.get('ats') not in ALLOWED_ATS:
        errors.append(f"{c.get('name')}: unsupported ATS {c.get('ats')}")
    if not c.get('name'):
        errors.append('company without name')
    if c.get('ats') == 'jsonld' and not c.get('url'):
        errors.append(f"{c.get('name')}: jsonld source has no url")
    if c.get('ats') != 'jsonld' and not c.get('token'):
        errors.append(f"{c.get('name')}: {c.get('ats')} source has no token")

for park in parks:
    if not park.get('key') or not park.get('label') or not park.get('keywords'):
        errors.append(f"invalid IT park: {park}")

if len(names) != len(set(names)):
    errors.append('duplicate company names detected')

print(f"Companies: {len(companies)}")
print(f"IT parks: {len(parks)}")
print(f"Job functions: {len(functions)}")
print(f"Industries: {len(industries)}")
print(f"ATS coverage: {sorted({c['ats'] for c in companies})}")
if errors:
    print("VALIDATION FAILED")
    for e in errors:
        print(" -", e)
    raise SystemExit(1)
print("VALIDATION PASSED")
