"""IT parks and global technology/business hubs loaded from it_parks.yaml."""
import yaml
from django.conf import settings


def _load_hubs():
    path = settings.BASE_DIR / "it_parks.yaml"
    try:
        with open(path, encoding="utf-8") as fh:
            rows = (yaml.safe_load(fh) or {}).get("it_parks", [])
    except (FileNotFoundError, OSError, yaml.YAMLError):
        rows = []
    out = {}
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        out[key] = {
            "label": row.get("label", key),
            "country": row.get("country"),
            "query": row.get("query", row.get("label", key)),
            "keywords": [str(x).casefold() for x in row.get("keywords", [])],
            "source": row.get("source"),
            "stats": row.get("stats", {}),
        }
    return out


HUBS = _load_hubs()


def hub_keys():
    return list(HUBS)


def clean_selection(selected):
    seen, out = set(), []
    for k in selected or []:
        if k in HUBS and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def matching_hubs(location, selected=None):
    loc = (location or "").casefold()
    if not loc:
        return []
    return [k for k in (selected or HUBS) if any(w in loc for w in HUBS[k]["keywords"])]
