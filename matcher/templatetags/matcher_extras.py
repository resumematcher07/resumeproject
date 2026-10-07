from django import template

from matcher.services.parser import BULLET

register = template.Library()


@register.filter
def unbullet(value):
    return BULLET.sub("", value or "").strip()


@register.filter
def entries(lines):
    from matcher.services.parser import group_entries, split_date

    out = []
    for e in group_entries(lines or []):
        head, date = [], ""
        for h in e["head"]:
            text, d = split_date(h)
            date = date or d
            head.append(text)
        out.append({"head": " · ".join(x.replace("|", "·") for x in head), "date": date, "bullets": e["bullets"]})
    return out
