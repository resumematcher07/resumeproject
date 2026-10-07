"""ATS-friendly LaTeX resume: generation, validation and safe PDF compilation.

ATS rules followed by the template: one column, standard section names, real selectable text (glyphtounicode),
no tables / images / icons / colours / text boxes, plain ASCII punctuation, dates on the same line as the role.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata

from .parser import RANGE, SINGLE, TITLE_RX, group_entries, split_date
from .skills_data import skill_category

# ---------- escaping ----------

_ASCII = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u2022": "-",
          "\u2026": "...", "\u2192": "->", "\u00d7": "x", "\u2265": ">=", "\u2264": "<=", "\u00a0": " ", "\u2212": "-"}
_TEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
        "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def esc(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = "".join(_ASCII.get(c, c) for c in text)
    text = "".join(c for c in text if c == "\n" or (0x20 <= ord(c) < 0x180))        # pdflatex-safe range
    return "".join(_TEX.get(c, c) for c in text).strip()


# ---------- template ----------

PREAMBLE = r"""\documentclass[10pt,letterpaper]{article}
% ATS-friendly resume: single column, selectable text, standard headings. Edit the content below freely.
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[left=0.65in,right=0.65in,top=0.55in,bottom=0.55in]{geometry}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage[hidelinks]{hyperref}
\usepackage{helvet}
\renewcommand{\familydefault}{\sfdefault}
\input{glyphtounicode}
\pdfgentounicode=1
\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{2pt}
\raggedright
\titleformat{\section}{\normalsize\bfseries\uppercase}{}{0em}{}[\titlerule]
\titlespacing*{\section}{0pt}{7pt}{3pt}
\setlist[itemize]{leftmargin=1.3em,itemsep=1pt,topsep=2pt,parsep=0pt}
"""


def _section(title, body):
    return f"\n\\section*{{{title}}}\n{body.strip()}\n" if body and body.strip() else ""


def _items(lines):
    lines = [l for l in lines if l and l.strip()]
    if not lines:
        return ""
    return "\\begin{itemize}\n" + "\n".join(f"  \\item {esc(l)}" for l in lines) + "\n\\end{itemize}"


def _entry_tex(entry, drop_links=False):
    date, flat = "", []
    for h in entry["head"]:
        text, d = split_date(h)
        date = date or d
        flat.extend(p.strip() for p in re.split(r"\s*\|\s*", text) if p.strip())
    if drop_links:
        flat = [p for p in flat if not re.fullmatch(r"(?i)(project[- ]?)?links?", p)]
        if flat:                                   # "Price Prediction SystemPython, XGBoost" -> title + tech stack
            m = re.match(r"^(.*?[a-z])(?=[A-Z][A-Za-z+#.]*,\s*[A-Z])(.+)$", flat[0])
            if m:
                flat[0:1] = [m.group(1), m.group(2)]
    out = []
    if flat:
        title = next((p for p in flat if TITLE_RX.search(p)), flat[0])
        rest = [p for p in flat if p != title]
        out.append(f"\\textbf{{{esc(title)}}}" + (f"\\hfill {esc(date)}" if date else ""))
        if rest:
            out.append(f"\\\\\\textit{{{esc(', '.join(rest))}}}")
    ul = _items(entry["bullets"])
    return "\n".join(out) + ("\n" if out and ul else "") + ul


def _entries_block(lines, drop_links=False):
    blocks = [_entry_tex(e, drop_links) for e in group_entries(lines)]
    return "\n\\vspace{3pt}\n".join(b for b in blocks if b.strip())


def _education_block(lines):
    out = []
    for l in lines:
        text, d = split_date(l)
        out.append(f"\\textbf{{{esc(text)}}}\\hfill {esc(d)}" if d else esc(l))
    return "\\\\\n".join(out)


def _skills_block(parsed, relevant, extra):
    rows, seen = [], set()
    if relevant:
        rows.append(("Key Skills for This Role", relevant))
        seen.update(relevant)
    groups = {}
    for cat, items in parsed.get("skills", {}).items():
        groups.setdefault(cat, []).extend(items)
    for s in extra:
        groups.setdefault(skill_category(s), []).append(s)
    for cat, items in groups.items():
        items = [s for s in dict.fromkeys(items) if s not in seen]
        if items:
            rows.append((cat, items))
    return "\\\\\n".join(f"\\textbf{{{esc(c)}:}} {esc(', '.join(i))}" for c, i in rows)


def build_latex(parsed, job=None, extra_skills=(), pursuing_certs=()):
    """Return a complete .tex document. `job` is an analysis result dict (see analysis.evaluate) or None."""
    extra, pursuing = [], []
    relevant = []
    if job:
        gaps = set(job.get("missing_required", []) + job.get("missing_preferred", []))
        extra = [s for s in extra_skills if s in gaps]                    # only skills the candidate confirmed
        pursuing = [c for c in pursuing_certs if c in {x["name"] for x in job.get("missing_certs", [])}]
        relevant = list(dict.fromkeys(job.get("matched", []) + extra))

    role = parsed.get("primary_title") or parsed.get("latest_title") or "Professional"
    label = parsed.get("experience_label", "")
    top = (relevant or parsed.get("skills_flat", []))[:6]
    if job:
        summary = role + (f" with {label} of experience" if parsed.get("experience_months") else "")
        if top:
            summary += " skilled in " + ", ".join(top[:-1]) + (" and " if len(top) > 1 else "") + top[-1]
        summary += f". Seeking the {job['title']} position at {job['company']}."
        if parsed.get("summary"):
            summary += " " + parsed["summary"]
    else:
        summary = parsed.get("summary") or (
            role + (f" with {label} of experience" if parsed.get("experience_months") else "")
            + (" skilled in " + ", ".join(top) if top else "") + ".")

    contact = " | ".join(esc(x) for x in [parsed.get("email"), parsed.get("phone"), *parsed.get("links", [])] if x)
    head = f"\\begin{{center}}\n{{\\LARGE\\bfseries {esc(parsed.get('name') or 'Candidate')}}}\\\\[3pt]\n{contact}\n\\end{{center}}\n"

    certs = list(parsed.get("certifications", []))
    cert_items = certs + [f"{c} (in progress)" for c in pursuing]

    body = [
        _section("Professional Summary", esc(summary)),
        _section("Skills", _skills_block(parsed, relevant, extra)),
        _section("Professional Experience", _entries_block(parsed.get("experience", []))),
        _section("Projects", _entries_block(parsed.get("projects", []), drop_links=True)),
        _section("Education", _education_block(parsed.get("education", []))),
        _section("Certifications", _items(cert_items)),
        _section("Leadership and Community", _entries_block(parsed.get("activities", []))),
        _section("Achievements", _items(parsed.get("achievements", []))),
    ]
    return PREAMBLE + "\n\\begin{document}\n" + head + "".join(body) + "\n\\end{document}\n"


# ---------- safe compilation ----------

ALLOWED_PACKAGES = {"fontenc", "inputenc", "geometry", "enumitem", "titlesec", "hyperref", "helvet", "lmodern", "xcolor",
                    "setspace", "microtype", "fancyhdr", "parskip", "mathptmx", "tgheros", "tgtermes", "charter", "url",
                    "multicol", "paralist", "etoolbox", "ragged2e"}
_FORBIDDEN = re.compile(
    r"\\(?:input|include|includeonly|openin|openout|read|readline|write|immediate|catcode|directlua|luaexec|special|"
    r"csname|endcsname|verbatiminput|lstinputlisting|InputIfFileExists|IfFileExists|newread|newwrite|openin|"
    r"pdf(?!gentounicode)[a-z]+|ShellEscape|scantokens|everyeof|detokenize)(?![A-Za-z])", re.I)   # TeX names end at a non-letter (\write18)
MAX_SOURCE = 200_000


def validate_source(source):
    """Return an error string if the LaTeX is not safe to compile, else None."""
    if len(source) > MAX_SOURCE:
        return "The LaTeX source is too large."
    cleaned = source.replace(r"\input{glyphtounicode}", "")
    m = _FORBIDDEN.search(cleaned)
    if m:
        return f"For security, the command {m.group(0)} is not allowed in this editor."
    if "run:" in cleaned.lower():
        return "For security, 'run:' links are not allowed."
    for pk in re.findall(r"\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{([^}]*)\}", cleaned):
        for name in (n.strip() for n in pk.split(",")):
            if name not in ALLOWED_PACKAGES:
                return f"Package '{name}' is not available in this editor (allowed: {', '.join(sorted(ALLOWED_PACKAGES))})."
    if r"\documentclass" not in cleaned or r"\begin{document}" not in cleaned:
        return r"The document must contain \documentclass and \begin{document}."
    return None


def compiler_available():
    return shutil.which("pdflatex") is not None


def _log_errors(log_text):
    lines = log_text.splitlines()
    out = []
    for i, l in enumerate(lines):
        if l.startswith("!"):
            out.append("\n".join(lines[i:i + 3]))
    return "\n\n".join(out[:4]) or "\n".join(lines[-12:])


def compile_pdf(source, timeout=30):
    """Compile LaTeX to PDF. Returns (pdf_bytes, None) or (None, error_message). Nothing is kept on disk."""
    err = validate_source(source)
    if err:
        return None, err
    exe = shutil.which("pdflatex")
    if not exe:
        return None, ("pdflatex is not installed on this server. Download the .tex file and compile it with Overleaf "
                      "or any LaTeX installation.")
    with tempfile.TemporaryDirectory(prefix="resume_tex_") as d:
        with open(os.path.join(d, "main.tex"), "w", encoding="utf-8") as fh:
            fh.write(source)
        env = dict(os.environ, openin_any="p", openout_any="p", shell_escape="f", TEXMFOUTPUT=d, TMPDIR=d, HOME=d)
        try:
            subprocess.run([exe, "-no-shell-escape", "-halt-on-error", "-interaction=nonstopmode",
                            "-output-directory", d, "main.tex"],
                           cwd=d, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "Compilation took too long and was stopped. Check for an infinite loop in your LaTeX."
        pdf = os.path.join(d, "main.pdf")
        if os.path.exists(pdf):
            with open(pdf, "rb") as fh:
                return fh.read(), None
        log = ""
        try:
            with open(os.path.join(d, "main.log"), encoding="utf-8", errors="ignore") as fh:
                log = _log_errors(fh.read())
        except OSError:
            pass
        return None, "LaTeX error:\n" + (log or "unknown error")
