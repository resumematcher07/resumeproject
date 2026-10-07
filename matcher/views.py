import threading
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.contrib import messages
from django.core.cache import cache
from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import GeneratedResume, Resume, purge_expired
from pathlib import Path

from .services import analysis, builder, hubs as hubs_mod, latex, parser

ALLOWED_EXT = {"pdf", "docx", "txt", "md"}


def _owner(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def _get(request, pk):
    """Fetch a resume that belongs to this browser session and hasn't expired."""
    purge_expired()
    # owner_session filter = only the browser session that uploaded this resume can ever open it (others get a 404)
    resume = get_object_or_404(Resume, pk=pk, owner_session=_owner(request))
    if getattr(settings, "RESUME_IDLE_SECONDS", 0):
        resume.touch()                                  # any request by the owner counts as "still here"
    return resume


def index(request):
    purge_expired()
    mine = Resume.objects.filter(owner_session=_owner(request)).order_by("-created_at").first()
    return render(request, "matcher/index.html", {"existing": mine, "ttl_minutes": settings.RESUME_TTL_SECONDS // 60})


@require_POST
def upload(request):
    f = request.FILES.get("resume")
    if not f:
        messages.error(request, "Please choose a resume file first.")
        return redirect("index")
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    if ext not in ALLOWED_EXT:
        messages.error(request, "Unsupported file type. Upload a PDF, DOCX or TXT resume.")
        return redirect("index")
    if f.size > settings.RESUME_MAX_BYTES:
        messages.error(request, "That file is larger than 5 MB.")
        return redirect("index")
    try:
        text = parser.extract_text(f, f.name)
    except Exception:
        messages.error(request, "We couldn't read that file. Is it a valid, un-encrypted PDF/DOCX?")
        return redirect("index")
    if len(text.split()) < 30:
        messages.error(request, "Very little text was found. Scanned/image-only PDFs aren't supported - upload a text-based file.")
        return redirect("index")

    orig = f.name[:200]
    # The uploaded file itself is NOT written to disk: it was read in memory above and only the extracted text +
    # parsed fields are kept (in the database) until the resume is deleted.
    resume = Resume.objects.create(
        owner_session=_owner(request), file="", original_name=orig,
        raw_text=text, parsed=parser.parse_resume(text),
    )
    return redirect("detail", pk=resume.pk)


def detail(request, pk):
    resume = _get(request, pk)
    return render(request, "matcher/detail.html", {
        "resume": resume, "p": resume.parsed, "hubs": hubs_mod.HUBS,
        "job_functions": analysis.job_functions_catalog(), "industries": analysis.industries_catalog()})


def _run_search(pk, hubs=None, job_functions=None, industries=None):
    """Runs the (slow) career-page search; progress is published through the cache for the progress bar."""
    key = f"search:{pk}"
    try:
        resume = Resume.objects.get(pk=pk)
        results, stats = analysis.search_and_rank(
            resume.parsed, hubs=hubs, job_functions=job_functions, industries=industries,
            progress=lambda d, t: cache.set(key, {"state": "running", "done": d, "total": t}, 600))
        # re-fetch: the resume may have been deleted while we were searching
        if Resume.objects.filter(pk=pk).update(analysis={"results": results, "stats": stats}):
            cache.set(key, {"state": "done"}, 600)
        else:
            cache.delete(key)
    except Exception:
        cache.set(key, {"state": "error"}, 600)
    finally:
        connection.close()


@require_POST
def search(request, pk):
    resume = _get(request, pk)
    key = f"search:{pk}"
    hubs = hubs_mod.clean_selection(request.POST.getlist("hub"))
    job_functions = [f for f in request.POST.getlist("job_function") if f in analysis.job_functions_catalog()]
    industries = [i for i in request.POST.getlist("industry") if i in analysis.industries_catalog()]
    if settings.RESUME_SYNC_SEARCH:
        _run_search(pk, hubs, job_functions, industries)
        return redirect("results", pk=resume.pk)
    if (cache.get(key) or {}).get("state") != "running":
        cache.set(key, {"state": "running", "done": 0, "total": 0}, 600)
        threading.Thread(target=_run_search, args=(pk, hubs, job_functions, industries), daemon=True).start()
    return JsonResponse({"state": "running", "status_url": request.build_absolute_uri(
        reverse("status", args=[pk])), "results_url": reverse("results", args=[pk])})


def status(request, pk):
    _get(request, pk)
    return JsonResponse(cache.get(f"search:{pk}") or {"state": "idle"})


def results(request, pk):
    resume = _get(request, pk)
    if not resume.analysis:
        return redirect("detail", pk=pk)
    return render(request, "matcher/results.html", {
        "resume": resume, "p": resume.parsed,
        "results": resume.analysis["results"], "stats": resume.analysis["stats"],
    })


@require_POST
def tailor(request, pk, job_key):
    resume = _get(request, pk)
    ev = next((r for r in resume.analysis.get("results", []) if r["key"] == job_key), None)
    if not ev:
        raise Http404
    data = builder.build_resume(
        resume.parsed, ev, ev,
        extra_skills=request.POST.getlist("have_skill"),
        pursuing_certs=request.POST.getlist("pursuing_cert"),
    )
    name = builder.safe_filename(resume.parsed.get("name", "resume"), ev)
    gen = GeneratedResume(resume=resume, job_key=job_key)
    gen.file.save(f"{uuid.uuid4().hex}.docx", ContentFile(data), save=True)
    # Read the file into the response before returning. Keeping a storage file
    # handle open in FileResponse can prevent cleanup on Windows (WinError 32)
    # when the generated resume is deleted during the same request/test run.
    with gen.file.open("rb") as fh:
        data = fh.read()
    response = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@require_POST
def ping(request, pk):
    """Heartbeat from the open page. Stops arriving when the tab is closed / the browser is force-stopped."""
    purge_expired()                                     # an already-expired resume must never be revived by a late ping
    resume = Resume.objects.filter(pk=pk, owner_session=_owner(request)).first()
    if resume is None:
        return JsonResponse({"alive": False}, status=404)
    resume.touch()
    return JsonResponse({"alive": True})


@require_POST
def leave(request, pk):
    """Sent with navigator.sendBeacon when the page is hidden/closed: start a short countdown to deletion."""
    resume = Resume.objects.filter(pk=pk, owner_session=_owner(request)).first()
    if resume is not None:
        resume.leaving()
    return HttpResponse(status=204)


@require_POST
def delete(request, pk):
    resume = _get(request, pk)
    for g in resume.generated.all():
        g.delete()
    resume.delete()
    messages.success(request, "Your resume and everything derived from it were permanently deleted.")
    return redirect("index")


# ---------- LaTeX (ATS-friendly) resume: generate -> edit -> PDF ----------

def _gen(request, pk, gid):
    resume = _get(request, pk)
    return resume, get_object_or_404(GeneratedResume, pk=gid, resume=resume)


@require_POST
def latex_new(request, pk):
    resume = _get(request, pk)
    job_key = request.POST.get("job_key", "")
    ev = next((r for r in resume.analysis.get("results", []) if r["key"] == job_key), None) if job_key else None
    source = latex.build_latex(resume.parsed, ev, request.POST.getlist("have_skill"), request.POST.getlist("pursuing_cert"))
    gen = GeneratedResume(resume=resume, job_key=job_key or "ats-base")
    gen.file.save(f"{uuid.uuid4().hex}.tex", ContentFile(source.encode("utf-8")), save=True)
    return redirect("editor", pk=resume.pk, gid=gen.pk)


def editor(request, pk, gid):
    resume, gen = _gen(request, pk, gid)
    job = next((r for r in resume.analysis.get("results", []) if r["key"] == gen.job_key), None)
    return render(request, "matcher/editor.html", {
        "resume": resume, "gen": gen, "source": Path(gen.file.path).read_text(encoding="utf-8"),
        "job": job, "compiler_ok": latex.compiler_available(),
    })


@require_POST
def editor_compile(request, pk, gid):
    resume, gen = _gen(request, pk, gid)
    source = request.POST.get("source", "")
    if len(source) <= latex.MAX_SOURCE:
        Path(gen.file.path).write_text(source, encoding="utf-8")        # edits persist for the resume's lifetime
    pdf, err = latex.compile_pdf(source)
    if err:
        return JsonResponse({"error": err}, status=422)
    return HttpResponse(pdf, content_type="application/pdf")


def editor_tex(request, pk, gid):
    resume, gen = _gen(request, pk, gid)
    name = builder.safe_filename(resume.parsed.get("name", "resume"), {"company": "ATS", "title": "resume"}).replace(".docx", ".tex")
    # Do not leave the .tex handle attached to a streaming response: on Windows
    # an open handle makes purge_expired() fail with WinError 32.
    with open(gen.file.path, "rb") as fh:
        data = fh.read()
    response = HttpResponse(data, content_type="application/x-tex")
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response
