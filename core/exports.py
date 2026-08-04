"""
Data export framework (roadmap: "Data Export Tools" — one-click CSV/PDF
export of a school's own records, with nothing locked in).

This module is deliberately the *only* place export logic lives: each
record type registers itself once, as an ExportType, and
core.views.export_panel / core.views.export_download are thin wrappers
around build_queryset()/write_csv() below rather than one-off export
code per data type (roadmap Phase 1: "a shared export utility ... that
individual record-type exporters plug into").

Scoping is the load-bearing part. Every ExportType declares
`school_field` — the lookup path from its model back to School — and
build_queryset() always runs the resulting queryset through
core.permissions.scope_to_school before anything else happens. That's
the same mechanism (and the same helper) every other cross-school
boundary in Notipa already uses, so an export can no more reach another
school's data than any other view in this app can. There is
deliberately no path through this module that skips scope_to_school.

PDF export and background-job handling for large exports (roadmap
Phases 3) aren't implemented yet — CSV only, generated synchronously,
which is Phase 2 of the roadmap's development plan. The registry shape
below (ExportType.fields as plain (header, getter) pairs, independent
of CSV specifically) is intended to be reusable once a PDF renderer is
added, rather than needing a parallel structure.
"""
import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from types import SimpleNamespace
from typing import Callable, Optional

from django.core.files.base import ContentFile
from django.http import HttpResponse
from django.utils import timezone
from fpdf import FPDF

from .models import (
    Announcement,
    ExportJob,
    FeeNotice,
    GuardianLink,
    Homework,
    PermissionSlipResponse,
    SchoolMembership,
    Student,
)
from .permissions import scope_to_school

logger = logging.getLogger(__name__)

# Above this many rows, export_download hands the job to a background
# thread (core.views.export_download / run_export_job below) instead of
# rendering inline — the roadmap's "a large school's full-year export
# doesn't time out a web request." A plain module constant rather than a
# per-school setting: every self-hosted install gets the same
# conservative default, and it's one line to change if a school's
# hardware genuinely needs a different cutoff.
BACKGROUND_EXPORT_ROW_THRESHOLD = 500


def _yesno(value):
    return "Yes" if value else "No"


def _fmt_dt(value):
    if not value:
        return ""
    return timezone_localize(value)


def timezone_localize(value):
    """Render a datetime the same simple way across every export column
    — date and time, no timezone-conversion surprises, since this is a
    human-readable export, not an API payload."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


@dataclass
class ExportType:
    """
    One exportable record type.

    - key: URL-safe identifier, e.g. "students".
    - label / description: shown in the export panel.
    - queryset_fn(request): returns the *unscoped* base queryset (with
      whatever select_related makes sense) — build_queryset() below is
      what applies scope_to_school, so an ExportType can never
      accidentally forget the school boundary by returning an
      already-scoped queryset here.
    - school_field: lookup path from the model back to School, passed
      straight through to scope_to_school's field_name.
    - fields: ordered (header, getter) pairs. getter takes one row
      instance and returns a string.
    - class_field: optional lookup path for the "limit to one class"
      filter, e.g. "school_class_id" or "student__school_class_id".
      None means this record type has no natural class filter.
    - date_field: optional lookup path (a DateField or DateTimeField)
      used for the "date range" filter — usually created_at, but a few
      record types filter on a more meaningful date instead (fee
      notices on due_date, for instance).
    """

    key: str
    label: str
    description: str
    queryset_fn: Callable
    school_field: str
    fields: list
    class_field: Optional[str] = None
    date_field: Optional[str] = None


def _students_queryset(request):
    return Student.objects.select_related("school_class").order_by("last_name", "first_name")


def _guardians_queryset(request):
    return (
        SchoolMembership.objects.filter(role=SchoolMembership.Role.GUARDIAN)
        .select_related("user")
        .order_by("user__last_name", "user__first_name")
    )


def _guardian_links_queryset(request):
    return (
        GuardianLink.objects.select_related("guardian", "student", "student__school_class")
        .order_by("student__last_name", "student__first_name")
    )


def _announcements_queryset(request):
    return Announcement.objects.select_related("school_class", "author").order_by("-created_at")


def _homework_queryset(request):
    return Homework.objects.select_related("school_class", "created_by").order_by("-created_at")


def _fee_notices_queryset(request):
    return FeeNotice.objects.select_related("student").order_by("due_date")


def _permission_slip_responses_queryset(request):
    return (
        PermissionSlipResponse.objects.select_related(
            "permission_slip", "student", "guardian"
        ).order_by("-permission_slip__created_at")
    )


REGISTRY = {}


def _register(export_type):
    REGISTRY[export_type.key] = export_type
    return export_type


_register(
    ExportType(
        key="students",
        label="Students",
        description="Every student record — name, class, date of birth, and status.",
        queryset_fn=_students_queryset,
        school_field="school",
        class_field="school_class_id",
        date_field="created_at",
        fields=[
            ("First Name", lambda s: s.first_name),
            ("Last Name", lambda s: s.last_name),
            ("Student ID", lambda s: s.student_id),
            ("Date of Birth", lambda s: _fmt_dt(s.date_of_birth)),
            ("Class", lambda s: s.school_class.name if s.school_class else ""),
            ("Status", lambda s: "Active" if s.is_active else "Archived"),
            ("Created At", lambda s: _fmt_dt(s.created_at)),
        ],
    )
)

_register(
    ExportType(
        key="guardians",
        label="Guardians",
        description="Every guardian account linked to this school, with contact details.",
        queryset_fn=_guardians_queryset,
        school_field="school",
        date_field="joined_at",
        fields=[
            ("Name", lambda m: m.user.get_full_name() or m.user.username),
            ("Username", lambda m: m.user.username),
            ("Phone Number", lambda m: m.user.phone_number),
            ("Email", lambda m: m.user.email),
            ("Status", lambda m: "Active" if m.is_active else "Revoked"),
            ("Joined At", lambda m: _fmt_dt(m.joined_at)),
        ],
    )
)

_register(
    ExportType(
        key="guardian_links",
        label="Guardian–Student Links",
        description="Which guardians are linked to which students, and the relationship.",
        queryset_fn=_guardian_links_queryset,
        school_field="student__school",
        class_field="student__school_class_id",
        date_field="created_at",
        fields=[
            ("Guardian Name", lambda gl: gl.guardian.get_full_name() or gl.guardian.username),
            ("Student Name", lambda gl: f"{gl.student.first_name} {gl.student.last_name}"),
            ("Class", lambda gl: gl.student.school_class.name if gl.student.school_class else ""),
            ("Relationship", lambda gl: gl.get_relationship_display()),
            ("Primary Contact", lambda gl: _yesno(gl.is_primary_contact)),
            ("Created At", lambda gl: _fmt_dt(gl.created_at)),
        ],
    )
)

_register(
    ExportType(
        key="announcements",
        label="Announcements",
        description="School-wide and class announcements, published or draft.",
        queryset_fn=_announcements_queryset,
        school_field="school",
        class_field="school_class_id",
        date_field="created_at",
        fields=[
            ("Title", lambda a: a.title),
            ("Scope", lambda a: a.school_class.name if a.school_class else "School-wide"),
            ("Author", lambda a: a.author.get_full_name() or a.author.username if a.author else ""),
            ("Published At", lambda a: _fmt_dt(a.published_at)),
            ("Created At", lambda a: _fmt_dt(a.created_at)),
        ],
    )
)

_register(
    ExportType(
        key="homework",
        label="Homework",
        description="Homework items posted to each class, with due dates.",
        queryset_fn=_homework_queryset,
        school_field="school_class__school",
        class_field="school_class_id",
        date_field="created_at",
        fields=[
            ("Title", lambda h: h.title),
            ("Class", lambda h: h.school_class.name),
            ("Due Date", lambda h: _fmt_dt(h.due_date)),
            ("Accepts Submissions", lambda h: _yesno(h.accepts_submissions)),
            (
                "Created By",
                lambda h: (h.created_by.get_full_name() or h.created_by.username)
                if h.created_by
                else "",
            ),
            ("Created At", lambda h: _fmt_dt(h.created_at)),
        ],
    )
)

_register(
    ExportType(
        key="fee_notices",
        label="Fee Notices",
        description="Fee due-date notices and their paid/unpaid/waived status.",
        queryset_fn=_fee_notices_queryset,
        school_field="school",
        date_field="due_date",
        fields=[
            ("Student", lambda f: f"{f.student.first_name} {f.student.last_name}"),
            ("Title", lambda f: f.title),
            ("Amount", lambda f: str(f.amount)),
            ("Currency", lambda f: f.currency),
            ("Due Date", lambda f: _fmt_dt(f.due_date)),
            ("Status", lambda f: f.get_status_display()),
            ("Created At", lambda f: _fmt_dt(f.created_at)),
        ],
    )
)

_register(
    ExportType(
        key="permission_slip_responses",
        label="Permission Slip Responses",
        description="Every guardian's response (or non-response) to each permission slip.",
        queryset_fn=_permission_slip_responses_queryset,
        school_field="permission_slip__school",
        class_field="permission_slip__school_class_id",
        date_field="responded_at",
        fields=[
            ("Permission Slip", lambda r: r.permission_slip.title),
            ("Student", lambda r: f"{r.student.first_name} {r.student.last_name}"),
            (
                "Guardian",
                lambda r: r.guardian.get_full_name() or r.guardian.username if r.guardian else "",
            ),
            ("Response", lambda r: r.get_response_display()),
            ("Notes", lambda r: r.notes),
            ("Responded At", lambda r: _fmt_dt(r.responded_at)),
        ],
    )
)


def get_export_types():
    """Ordered list of every registered ExportType, for the export panel."""
    return list(REGISTRY.values())


def _scoped_queryset_for_school(export_type, school, class_id=None, date_from=None, date_to=None):
    """
    The one place school-scoping + filtering actually happens. Takes a
    plain School instance rather than a request, so it works identically
    whether it's called synchronously from a view (which has a real
    request) or later from a background thread running run_export_job
    (which doesn't). Still goes through core.permissions.scope_to_school
    — via a tiny request-shaped stand-in with just a `.school` attribute
    — rather than filtering directly, so there is exactly one code path
    that decides whether a row belongs to a school, used everywhere.
    """
    queryset = scope_to_school(
        export_type.queryset_fn(None), SimpleNamespace(school=school), field_name=export_type.school_field
    )

    if class_id and export_type.class_field:
        queryset = queryset.filter(**{export_type.class_field: class_id})

    if export_type.date_field:
        if date_from:
            queryset = queryset.filter(**{f"{export_type.date_field}__gte": date_from})
        if date_to:
            queryset = queryset.filter(**{f"{export_type.date_field}__lte": date_to})

    return queryset


def build_queryset(export_type, request, class_id=None, date_from=None, date_to=None):
    """
    Build the fully-scoped, fully-filtered queryset for one export from
    a view's request. Returns queryset.none() if request has no active
    school, same as scope_to_school itself — the view-facing entry
    point; core.views.export_download uses this directly, and it's what
    run_export_job's request-less counterpart (_scoped_queryset_for_school
    above) mirrors for the background-job path.
    """
    if request.school is None:
        return export_type.queryset_fn(request).none()
    return _scoped_queryset_for_school(
        export_type, request.school, class_id=class_id, date_from=date_from, date_to=date_to
    )


def _safe_filename_part(text):
    """Strip anything that isn't filesystem/URL-friendly out of a
    filename component (school name, export label) — exports are
    downloaded straight from the browser, so this needs to survive
    Content-Disposition unescaped."""
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return text or "export"


def export_filename(export_type, format_key, school_name):
    """One naming scheme for every export, CSV or PDF, whether it's
    downloaded immediately or generated by a background ExportJob."""
    return (
        f"{_safe_filename_part(school_name)}-{export_type.key}-"
        f"{date.today().isoformat()}.{format_key}"
    )


def csv_bytes(export_type, rows):
    """
    Render `rows` (an iterable of model instances) as CSV bytes using
    export_type.fields. This is the one place CSV formatting happens —
    every CSV exporter, sync or background, gets the same
    quoting/escaping behaviour for free rather than reimplementing it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header for header, _ in export_type.fields])
    for obj in rows:
        writer.writerow([getter(obj) for _, getter in export_type.fields])
    return buffer.getvalue().encode("utf-8")



# fpdf2's built-in "core" fonts (helvetica, etc.) are the 14 standard PDF
# fonts, which only support latin-1 — no em dashes, curly quotes, or any
# script outside Western-European Latin. Embedding a real Unicode TTF
# would fix that properly, but it means shipping (and keeping updated) a
# font file in the repo, which cuts against the roadmap's explicit ask
# for something "lightweight" and "dependency-light" for a self-hosted
# install. _pdf_safe() is the tradeoff: common punctuation degrades to
# its closest ASCII equivalent, and anything else outside latin-1
# (accented Latin characters are fine; non-Latin scripts aren't) is
# replaced rather than crashing the export. CSV export is unaffected —
# full Unicode, since it's plain text, not a rendered font.
_PDF_UNICODE_FALLBACKS = {
    "—": "-",
    "–": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
}


def _pdf_safe(value):
    text = str(value)
    for src, dst in _PDF_UNICODE_FALLBACKS.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def pdf_bytes(export_type, rows, school_name):
    """
    Render `rows` as a simple landscape table PDF — export_type.label as
    a title, export_type.fields as the table's columns, using fpdf2 (a
    pure-Python, dependency-light PDF library — no headless browser,
    the roadmap's explicit ask for something that suits a self-hosted
    install running on modest hardware). fpdf2's table() context
    manager paginates automatically once a roster/summary runs past one
    page, so nothing here needs to track page breaks by hand.

    This is deliberately the same generic table shape for every
    ExportType rather than a bespoke layout per record type — the
    roadmap's "printable class roster" and "permission-slip response
    summary" are exactly what a Students or Permission Slip Responses
    export already looks like as a table, so one renderer covers both
    without extra code per record type.
    """
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    title = _pdf_safe(f"{school_name} - {export_type.label}")
    pdf.set_title(title)
    pdf.add_page()

    pdf.set_font("helvetica", style="B", size=14)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=9)
    pdf.cell(
        0,
        6,
        f"Generated {timezone.localdate().isoformat()}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)

    pdf.set_font("helvetica", size=8)
    header = [_pdf_safe(header) for header, _ in export_type.fields]
    data = [header] + [
        [_pdf_safe(getter(obj)) for _, getter in export_type.fields] for obj in rows
    ]
    with pdf.table(data):
        pass

    return bytes(pdf.output())


def render_bytes(export_type, rows, format_key, school_name):
    """Dispatch to csv_bytes/pdf_bytes and return (content, content_type)
    — the one place that maps a format key to a renderer, used by both
    the synchronous download view and run_export_job below."""
    if format_key == ExportJob.Format.PDF:
        return pdf_bytes(export_type, rows, school_name), "application/pdf"
    return csv_bytes(export_type, rows), "text/csv"


def http_response(export_type, queryset, format_key, school_name):
    """Render `queryset` immediately and wrap it as a downloadable
    HttpResponse — the synchronous path, used whenever a queryset is
    under BACKGROUND_EXPORT_ROW_THRESHOLD."""
    content, content_type = render_bytes(export_type, queryset, format_key, school_name)
    response = HttpResponse(content, content_type=content_type)
    filename = export_filename(export_type, format_key, school_name)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def run_export_job(job_id):
    """
    Generate the file for one ExportJob and save it — the background
    half of the "move exports above a size threshold to a background
    task" roadmap item. Dispatched from core.views.export_download,
    normally on a plain daemon thread (see that view's docstring for why
    there's no Celery/Redis here); test_settings.EXPORT_JOBS_RUN_SYNC
    makes the same view call this inline instead, so tests don't need to
    sleep/poll for a thread to finish.

    Looked up by id (not passed the ExportJob instance) so this can be
    handed to threading.Thread(...) as a target without holding a
    reference to a model instance across a thread boundary, and so a
    second call (e.g. a future manual retry) always starts from the
    row's current state in the database rather than a stale in-memory
    copy.

    Never raises back to its caller — a background thread has no
    request/response to report a traceback through, so any failure is
    caught, logged, and recorded on the job itself (status FAILED,
    error_message) for the admin to see on the export panel instead.
    """
    try:
        job = ExportJob.objects.select_related("school", "requested_by").get(pk=job_id)
    except ExportJob.DoesNotExist:  # pragma: no cover — defensive only
        logger.error("run_export_job: ExportJob %s not found", job_id)
        return

    job.status = ExportJob.Status.RUNNING
    job.save(update_fields=["status"])

    try:
        export_type = REGISTRY[job.export_key]
        queryset = _scoped_queryset_for_school(
            export_type,
            job.school,
            class_id=job.filters.get("class_id"),
            date_from=parse_iso_date(job.filters.get("date_from")),
            date_to=parse_iso_date(job.filters.get("date_to")),
        )
        rows = list(queryset)
        content, _ = render_bytes(export_type, rows, job.format, job.school.name)
        filename = export_filename(export_type, job.format, job.school.name)

        job.file.save(filename, ContentFile(content), save=False)
        job.row_count = len(rows)
        job.status = ExportJob.Status.READY
        job.completed_at = timezone.now()
        job.save(update_fields=["file", "row_count", "status", "completed_at"])

        if job.requested_by is not None:
            from .push import notify_users  # local import: avoids a hard

            # dependency on core.push at module load for what's otherwise
            # a pure export module — same "best-effort, never blocks the
            # real work" posture push already has everywhere else it's
            # called from.
            notify_users(
                [job.requested_by],
                title="Export ready",
                body=f"Your {export_type.label} export is ready to download.",
                url="/exports/",
            )
    except Exception as exc:  # pragma: no cover — defensive: a broken
        # exporter must never leave a job stuck on RUNNING forever.
        logger.exception("run_export_job failed for job %s", job_id)
        job.status = ExportJob.Status.FAILED
        job.error_message = str(exc)
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "error_message", "completed_at"])


def parse_iso_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
