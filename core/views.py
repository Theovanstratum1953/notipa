import json
from datetime import date, timedelta

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    AnnouncementForm,
    FeeNoticeForm,
    GuardianEditForm,
    GuardianForm,
    GuardianLinkForm,
    HomeworkForm,
    HomeworkSubmissionForm,
    PermissionSlipForm,
    ReportCardForm,
    report_card_entry_formset_factory,
    SchoolCalendarEventForm,
    SchoolClassForm,
    SchoolForm,
    SchoolSettingsForm,
    StudentForm,
    StudentLinkForm,
    TeacherEditForm,
    TeacherForm,
    TermForm,
)
from .messaging import (
    _teacher_ids_for_class,
    class_messaging_effectively_enabled,
    thread_messaging_enabled,
)
from .models import (
    Announcement,
    AnnouncementRead,
    AttendanceRecord,
    FeeNotice,
    GuardianLink,
    Homework,
    HomeworkSubmission,
    Message,
    MessageThread,
    MessageThreadRead,
    PermissionSlip,
    PermissionSlipResponse,
    PushSubscription,
    ReportCard,
    ReportCardEntry,
    ReportCardRead,
    School,
    SchoolCalendarEvent,
    SchoolClass,
    SchoolMembership,
    Student,
    Term,
)
from .permissions import role_required, scope_to_school, superuser_required
from .push import notify_users, push_configured

User = get_user_model()


def _guardian_users_for_students(students):
    """
    Every distinct guardian (User) linked to any student in `students` —
    the recipient set for a push notification about content scoped to
    those students (a new announcement, homework item, permission slip,
    etc.). Shared by every notify_users() call site in this file rather
    than each recomputing the same guardian_links join, so "who gets
    notified" always means the same query everywhere it's asked.
    """
    return User.objects.filter(guardian_links__student__in=students).distinct()


@login_required
def dashboard(request):
    """
    Overview page, scoped to the logged-in user's active school
    (request.school, set by core.middleware.ActiveSchoolMiddleware).

    A user with no active membership yet (freshly created, not linked
    to a school) sees an explicit "not linked to a school" state rather
    than empty-looking zeros — those two situations look identical if
    you don't distinguish them, and a new user staring at "0 students"
    can't tell whether that's true or whether something's broken.

    A guardian sees a different page entirely (guardian_dashboard),
    since school-wide counts (total students, total classes) aren't
    meaningful or appropriate for them to see — a guardian's "overview"
    is their own children, not the school's roster.
    """
    if request.school is None:
        return render(request, "core/dashboard.html", {"no_school": True})

    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        return _guardian_dashboard(request)

    announcements = scope_to_school(Announcement.objects.all(), request)
    pending_responses = scope_to_school(
        PermissionSlipResponse.objects.filter(
            response=PermissionSlipResponse.Response.PENDING
        ),
        request,
        field_name="permission_slip__school",
    )

    context = {
        "no_school": False,
        "student_count": scope_to_school(Student.objects.filter(is_active=True), request).count(),
        "class_count": scope_to_school(SchoolClass.objects.filter(is_active=True), request).count(),
        "announcement_count": announcements.count(),
        "pending_response_count": pending_responses.count(),
        "recent_announcements": announcements.select_related(
            "school", "school_class"
        ).order_by("-created_at")[:5],
    }
    return render(request, "core/dashboard.html", context)


# ---------------------------------------------------------------------
# Guardian-facing views — before this, a guardian account could log in
# and be linked to a student (Sections 19-20), but had nowhere to
# actually go: every People page is admin/teacher-only, and the
# ordinary dashboard's school-wide counts aren't theirs to see. This
# gives a guardian something to land on (their own children) and a
# read-only page per child. Announcements/Homework/etc. still don't
# exist as real views yet (Section 10), so this is deliberately limited
# to what already has real data behind it: the student/class/teacher
# relationship a guardian cares most about.
# ---------------------------------------------------------------------

def _guardian_dashboard(request):
    guardian_links = (
        request.user.guardian_links.filter(student__school=request.school)
        .select_related("student__school_class__homeroom_teacher__user")
        .order_by("-is_primary_contact", "student__last_name", "student__first_name")
    )
    return render(
        request, "core/guardian_dashboard.html", {"no_school": False, "guardian_links": guardian_links}
    )


@login_required
@role_required(SchoolMembership.Role.GUARDIAN)
def my_child_detail(request, pk):
    """
    Read-only view of one of the requesting guardian's own children —
    the guardian-facing counterpart to student_detail, which is
    admin/teacher-only and lets you edit/archive/manage guardian links.
    A guardian can look but can't touch: no edit, no archive, no adding
    or removing guardian links from here.

    Scoped two ways: role_required(GUARDIAN) restricts this to guardian
    accounts at all, and the GuardianLink lookup below restricts it
    further to students *this* guardian is actually linked to — so a
    guardian can't view another family's child by guessing a student id,
    even within the same school.
    """
    link = get_object_or_404(
        GuardianLink.objects.select_related(
            "student__school_class__homeroom_teacher__user"
        ).filter(student__school=request.school),
        guardian=request.user,
        student_id=pk,
    )
    student = link.student
    announcements = (
        Announcement.objects.filter(school=request.school, published_at__isnull=False)
        .filter(Q(school_class__isnull=True) | Q(school_class=student.school_class))
        .order_by("-published_at")[:5]
    )
    homework_items = (
        Homework.objects.filter(school_class=student.school_class)
        .order_by("-created_at")[:5]
        if student.school_class
        else Homework.objects.none()
    )
    fee_notices = FeeNotice.objects.filter(student=student).order_by("due_date")[:5]
    permission_slip_responses = (
        PermissionSlipResponse.objects.filter(student=student)
        .select_related("permission_slip")
        .order_by("permission_slip__response_deadline", "-permission_slip__created_at")[:5]
    )
    # Homeroom + co-teachers of this child's class — the people a
    # "Message" button on this page is allowed to reach, per
    # message_thread_start's own connection check. Empty (and the
    # template hides the section entirely) if messaging isn't
    # effectively enabled (school switch off, or this specific class has
    # opted out — core.messaging.class_messaging_effectively_enabled),
    # or the child has no class assigned yet. These are the "start a
    # conversation" entry points the proposal says should be hidden when
    # off — an already-existing thread stays reachable read-only via the
    # Messages inbox regardless (core.views.messages_inbox), which isn't
    # gated on this at all.
    connected_teachers = []
    class_thread = None
    if class_messaging_effectively_enabled(student.school_class):
        connected_teachers = list(
            User.objects.filter(id__in=_connected_teacher_ids_for_student(student)).order_by(
                "last_name", "first_name"
            )
        )
        # The child's class-group thread, if this guardian is currently a
        # participant in it (core.messaging.sync_class_thread keeps that
        # in sync automatically) — None if the class thread doesn't exist
        # yet (e.g. no messaging, or the sync hasn't run for some reason),
        # not just if this guardian happens to have no children in it.
        class_thread = MessageThread.objects.filter(
            school=request.school,
            thread_type=MessageThread.ThreadType.CLASS,
            school_class_id=student.school_class_id,
            participants=request.user,
        ).first()
    # Recent attendance (roadmap: Attendance Tracking) — this guardian's
    # own child only, same GuardianLink scoping as everything else on
    # this page; the full history lives on my_child_attendance.
    attendance_records = AttendanceRecord.objects.filter(student=student).order_by("-date")[:5]

    # Recent published report cards (roadmap: Report Cards per Student)
    # — same "own child only" scoping; the full history lives on
    # my_child_report_cards.
    report_cards = (
        ReportCard.objects.filter(student=student, status=ReportCard.Status.PUBLISHED)
        .select_related("term")
        .order_by("-term__start_date")[:5]
    )

    return render(
        request,
        "core/my_child_detail.html",
        {
            "link": link,
            "student": student,
            "announcements": announcements,
            "homework_items": homework_items,
            "fee_notices": fee_notices,
            "permission_slip_responses": permission_slip_responses,
            "connected_teachers": connected_teachers,
            "class_thread": class_thread,
            "attendance_records": attendance_records,
            "report_cards": report_cards,
        },
    )


@login_required
def placeholder(request, title, message):
    """Generic stand-in page for sidebar sections that don't have a real
    view yet. Nothing currently routes here — Announcements, Homework,
    Fee Notices, Permission Slips, and Settings have all since been
    replaced with real views (see their own sections) — but the helper
    is kept as ready-made scaffolding for the next section that starts
    out as a placeholder (e.g. whatever PWA/push settings need)."""
    return render(request, "core/placeholder.html", {"title": title, "message": message})


# ---------------------------------------------------------------------
# Announcements — Phase 1 build sequence item 4, the first real content
# section (Homework/Fee Notices/Permission Slips are still placeholders
# — Section 10). School-wide (school_class is null) or class-scoped,
# with a draft/published split: creating one only saves a draft, never
# visible to guardians until a separate "Publish" action is taken, so
# "save my edits" and "make this visible to families" can't be
# accidentally conflated into one click. Restricted to admin/teacher to
# write; a guardian only ever sees published ones, filtered to
# school-wide plus whichever classes their own linked children are in —
# never another family's class.
# ---------------------------------------------------------------------

@login_required
def announcements_list(request):
    """
    Dispatches to the admin/teacher view or the guardian view depending
    on the requesting user's role — same branching pattern as
    core.views.dashboard's guardian split, and for the same reason: an
    admin/teacher's "all announcements including drafts, with edit
    controls" and a guardian's "published ones relevant to my kids,
    read-only" are different enough pages that forcing them through one
    template would mean permission checks scattered through the
    template instead of the view.
    """
    if request.school is None:
        messages.error(request, "You need to be linked to a school to see announcements.")
        return redirect("core:dashboard")

    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        return _guardian_announcements(request)

    announcements = scope_to_school(
        Announcement.objects.select_related("school_class", "author"), request
    ).order_by("-created_at")
    return render(request, "core/announcements_list.html", {"announcements": announcements})


def _relevant_class_ids_for_guardian(request):
    """The SchoolClass ids of every child linked to the requesting
    guardian at request.school — used to decide which class-scoped
    announcements (and, later, homework/fee notices/permission slips)
    a guardian is allowed to see. A guardian with no linked children
    yet simply sees nothing class-specific, only school-wide posts."""
    return list(
        request.user.guardian_links.filter(student__school=request.school).values_list(
            "student__school_class_id", flat=True
        )
    )


def _guardian_announcements(request):
    class_ids = _relevant_class_ids_for_guardian(request)
    announcements = list(
        Announcement.objects.filter(school=request.school, published_at__isnull=False)
        .filter(Q(school_class__isnull=True) | Q(school_class_id__in=class_ids))
        .select_related("school_class")
        .order_by("-published_at")
    )
    read_ids = set(
        AnnouncementRead.objects.filter(
            guardian=request.user, announcement__in=announcements
        ).values_list("announcement_id", flat=True)
    )
    for announcement in announcements:
        announcement.is_read = announcement.id in read_ids
    return render(
        request, "core/guardian_announcements.html", {"announcements": announcements}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def announcement_new(request):
    if request.school is None:
        messages.error(
            request, "You need to be linked to a school before you can add an announcement."
        )
        return redirect("core:dashboard")

    if request.method == "POST":
        form = AnnouncementForm(request.POST, school=request.school)
        if form.is_valid():
            announcement = form.save(commit=False)
            announcement.author = request.user
            announcement.save()
            messages.success(
                request,
                f"“{announcement.title}” saved as a draft — guardians won't see it until "
                f"you publish it.",
            )
            return redirect("core:announcements")
    else:
        form = AnnouncementForm(school=request.school)

    return render(request, "core/announcement_form.html", {"form": form})


def _get_announcement_or_404(request, pk):
    return get_object_or_404(scope_to_school(Announcement.objects.all(), request), pk=pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def announcement_edit(request, pk):
    announcement = _get_announcement_or_404(request, pk)

    if request.method == "POST":
        form = AnnouncementForm(request.POST, instance=announcement, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(request, f"“{announcement.title}” updated.")
            return redirect("core:announcements")
    else:
        form = AnnouncementForm(instance=announcement, school=request.school)

    return render(
        request, "core/announcement_form.html", {"form": form, "announcement": announcement}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def announcement_publish(request, pk):
    announcement = _get_announcement_or_404(request, pk)
    announcement.published_at = timezone.now()
    announcement.save(update_fields=["published_at"])

    students = Student.objects.filter(school=announcement.school, is_active=True)
    if announcement.school_class_id:
        students = students.filter(school_class_id=announcement.school_class_id)
    notify_users(
        _guardian_users_for_students(students),
        title="New announcement",
        body=announcement.title,
        url=reverse("core:announcements"),
    )

    messages.success(request, f"“{announcement.title}” published — guardians can now see it.")
    return redirect("core:announcements")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def announcement_unpublish(request, pk):
    announcement = _get_announcement_or_404(request, pk)
    announcement.published_at = None
    announcement.save(update_fields=["published_at"])
    messages.success(
        request, f"“{announcement.title}” unpublished — back to a draft, hidden from guardians."
    )
    return redirect("core:announcements")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def announcement_delete(request, pk):
    """
    A real delete, not an archive — unlike Teachers/Guardians/Classes/
    Students, Announcement has no is_active field and nothing else in
    the schema references one the way homeroom_teacher references a
    SchoolMembership, so there's no "history" a soft delete would be
    protecting. Deleting one also cascades to its AnnouncementRead rows
    (read receipts for a post that no longer exists have no reason to
    stick around).
    """
    announcement = _get_announcement_or_404(request, pk)
    title = announcement.title
    announcement.delete()
    messages.success(request, f"“{title}” deleted.")
    return redirect("core:announcements")


@login_required
@role_required(SchoolMembership.Role.GUARDIAN)
@require_POST
def announcement_mark_read(request, pk):
    """
    Records that the requesting guardian has read a given announcement
    (core.models.AnnouncementRead). Re-derives the same "published,
    school-wide or one of my kids' classes" visibility rule
    _guardian_announcements uses, rather than trusting that a pk in the
    URL is one this guardian was actually shown — otherwise a guardian
    could mark an announcement from an unrelated class (or an
    unpublished draft) as read by guessing its id.
    """
    class_ids = _relevant_class_ids_for_guardian(request)
    announcement = get_object_or_404(
        Announcement.objects.filter(school=request.school, published_at__isnull=False).filter(
            Q(school_class__isnull=True) | Q(school_class_id__in=class_ids)
        ),
        pk=pk,
    )
    AnnouncementRead.objects.get_or_create(announcement=announcement, guardian=request.user)
    return redirect("core:announcements")


# ---------------------------------------------------------------------
# Homework — Phase 1 build sequence item 5, following the same
# admin/teacher-write, guardian-read shape Announcements (Section 23)
# established. The one real difference: Homework is always class-scoped
# (the model's `school_class` isn't nullable — there's no "school-wide
# homework" the way there's a school-wide announcement), and there's no
# draft/publish split or read-tracking model here, so it's a simpler
# feature than Announcements in both directions — it becomes visible to
# a class's guardians the moment it's saved, and there's no per-guardian
# "read" state to track.
# ---------------------------------------------------------------------

@login_required
def homework_list(request):
    """Dispatches by role, same pattern as announcements_list/dashboard."""
    if request.school is None:
        messages.error(request, "You need to be linked to a school to see homework.")
        return redirect("core:dashboard")

    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        return _guardian_homework(request)

    homework_items = scope_to_school(
        Homework.objects.select_related("school_class", "created_by"),
        request,
        field_name="school_class__school",
    ).order_by("-created_at")
    return render(request, "core/homework_list.html", {"homework_items": homework_items})


def _guardian_homework(request):
    """
    Same base list _relevant_class_ids_for_guardian always built, plus
    (new, for submissions) one submission row per (homework, own child)
    pair for any homework that accepts submissions — flattened onto each
    homework item as `.my_submission_rows` rather than fanning the whole
    list out per-child, since most homework display (title/description/
    due date) is still per-class, not per-child; only the upload/status
    section underneath needs to repeat once per one of this guardian's
    own children in that class (handles the sibling-in-same-class case,
    where each child needs their own file).

    Query-layer scoping, not just template hiding: submissions and
    my_children_by_class are both built strictly from this guardian's
    own GuardianLink rows, so nothing here can surface another family's
    child or submission even for a class this guardian legitimately has
    a kid in.
    """
    class_ids = _relevant_class_ids_for_guardian(request)
    homework_items = list(
        Homework.objects.filter(school_class_id__in=class_ids)
        .select_related("school_class")
        .order_by("-created_at")
    )

    my_children_by_class = {}
    for link in request.user.guardian_links.filter(student__school=request.school).select_related(
        "student"
    ):
        my_children_by_class.setdefault(link.student.school_class_id, []).append(link.student)

    all_my_children_ids = [
        student.id for students in my_children_by_class.values() for student in students
    ]
    submissions_by_key = {
        (s.homework_id, s.student_id): s
        for s in HomeworkSubmission.objects.filter(
            homework__in=homework_items, student_id__in=all_my_children_ids
        )
    }

    today = timezone.localdate()
    for homework in homework_items:
        homework.my_submission_rows = []
        if not homework.accepts_submissions:
            continue
        for student in my_children_by_class.get(homework.school_class_id, []):
            submission = submissions_by_key.get((homework.id, student.id))
            past_due = bool(homework.due_date and today > homework.due_date)
            if submission:
                status = submission.status
                can_replace = not past_due
            else:
                status = "missing" if past_due else "not_submitted"
                can_replace = True  # a first submission is always allowed, even late
            homework.my_submission_rows.append(
                {
                    "student": student,
                    "submission": submission,
                    "status": status,
                    "can_replace": can_replace,
                }
            )

    return render(
        request, "core/guardian_homework.html", {"homework_items": homework_items}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def homework_new(request):
    if request.school is None:
        messages.error(
            request, "You need to be linked to a school before you can post homework."
        )
        return redirect("core:dashboard")

    if request.method == "POST":
        form = HomeworkForm(request.POST, request.FILES, school=request.school)
        if form.is_valid():
            homework = form.save(commit=False)
            homework.created_by = request.user
            homework.save()

            notify_users(
                _guardian_users_for_students(
                    Student.objects.filter(school_class=homework.school_class, is_active=True)
                ),
                title="New homework",
                body=f"{homework.title} — {homework.school_class.name}",
                # Points at the list page (core:homework), not
                # homework_detail — that per-student roster view is
                # admin/teacher-only, and a guardian tapping this
                # notification would otherwise land on a 403.
                url=reverse("core:homework"),
            )

            messages.success(
                request,
                f"“{homework.title}” posted to {homework.school_class.name} — visible to "
                f"that class's guardians now.",
            )
            return redirect("core:homework")
    else:
        form = HomeworkForm(school=request.school)

    return render(request, "core/homework_form.html", {"form": form})


def _get_homework_or_404(request, pk):
    return get_object_or_404(
        scope_to_school(
            Homework.objects.all(), request, field_name="school_class__school"
        ),
        pk=pk,
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def homework_detail(request, pk):
    """
    Per-student submission roster for one homework item — the teacher/
    admin-facing counterpart to permission_slip_detail, built the same
    way: one row per active student currently in the class, scoped via
    _get_homework_or_404 the same as homework_edit/homework_delete so
    this can't be reached for another school's homework by guessing an
    id.

    submitted/late/missing is derived here, not stored anywhere as a
    manual "missing" state: a student with no HomeworkSubmission row is
    "missing" once the due date has passed, or simply "not yet submitted"
    before it — HomeworkSubmission.status itself only ever distinguishes
    submitted from late, since both of those already imply a row exists
    (see the model's docstring).

    Renders even when accepts_submissions is off — the roster section
    just won't mean much until a teacher turns it on for this item — so
    this page is a safe, permanent detail URL for a homework item rather
    than one that only exists conditionally.
    """
    homework = _get_homework_or_404(request, pk)
    students = Student.objects.filter(
        school_class=homework.school_class, is_active=True
    ).order_by("last_name", "first_name")
    submissions_by_student = {
        s.student_id: s
        for s in HomeworkSubmission.objects.filter(homework=homework).select_related(
            "submitted_by"
        )
    }
    today = timezone.localdate()
    roster = []
    for student in students:
        submission = submissions_by_student.get(student.id)
        if submission:
            status = submission.status
        elif homework.due_date and today > homework.due_date:
            status = "missing"
        else:
            status = "not_submitted"
        roster.append({"student": student, "submission": submission, "status": status})

    return render(
        request, "core/homework_detail.html", {"homework": homework, "roster": roster}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def homework_edit(request, pk):
    homework = _get_homework_or_404(request, pk)

    if request.method == "POST":
        form = HomeworkForm(
            request.POST, request.FILES, instance=homework, school=request.school
        )
        if form.is_valid():
            form.save()
            messages.success(request, f"“{homework.title}” updated.")
            return redirect("core:homework")
    else:
        form = HomeworkForm(instance=homework, school=request.school)

    return render(
        request, "core/homework_form.html", {"form": form, "homework": homework}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def homework_delete(request, pk):
    """
    A real delete, same reasoning as announcement_delete: Homework has
    no is_active field and nothing else references it, so there's no
    history a soft delete would be protecting.
    """
    homework = _get_homework_or_404(request, pk)
    title = homework.title
    homework.delete()
    messages.success(request, f"“{title}” deleted.")
    return redirect("core:homework")


@login_required
@role_required(SchoolMembership.Role.GUARDIAN)
@require_POST
def homework_submit(request, pk, student_pk):
    """
    Records (or replaces) a guardian's submission for one of their own
    linked children against one homework item. Re-verifies both the
    homework's accepts_submissions flag and the GuardianLink fresh from
    the database, rather than trusting that a (pk, student_pk) pair in
    the URL is one this guardian was actually shown — same reasoning as
    permission_slip_respond: a guardian shouldn't be able to submit on
    behalf of an unrelated student, or against a homework item that
    doesn't accept submissions, by guessing ids in a POST.

    Any guardian linked to the student can submit or replace (multi-
    guardian household support — GuardianLink docstring), so this
    doesn't check that request.user is *the* guardian on any existing
    HomeworkSubmission row, only that they're *a* guardian of this
    student.

    Replacing an existing submission is blocked once the due date has
    passed — "can replace it any time before the due date passes" — but
    a first-ever submission is always accepted, even late, since real
    households run late and a late submission is still meant to be
    accepted and flagged, not turned away.
    """
    homework = get_object_or_404(
        Homework.objects.filter(
            school_class__school=request.school, accepts_submissions=True
        ),
        pk=pk,
    )
    if not GuardianLink.objects.filter(guardian=request.user, student_id=student_pk).exists():
        raise Http404

    existing = HomeworkSubmission.objects.filter(
        homework=homework, student_id=student_pk
    ).first()
    today = timezone.localdate()
    past_due = bool(homework.due_date and today > homework.due_date)
    if existing and past_due:
        messages.error(
            request,
            "The due date has passed, so this submission can no longer be replaced.",
        )
        return redirect("core:homework")

    old_file = existing.file if existing else None
    form = HomeworkSubmissionForm(request.POST, request.FILES, instance=existing)
    if not form.is_valid():
        error_text = " ".join(
            " ".join(errs) for errs in form.errors.values()
        ) or "Couldn't save that submission — check the file and try again."
        messages.error(request, error_text)
        return redirect("core:homework")

    submission = form.save(commit=False)
    submission.homework = homework
    submission.student_id = student_pk
    submission.submitted_by = request.user
    submission.submitted_at = timezone.now()
    submission.status = (
        HomeworkSubmission.Status.LATE if past_due else HomeworkSubmission.Status.SUBMITTED
    )
    submission.save()

    # Clean up the previous file on disk on a replace, rather than
    # leaving it orphaned in storage — schools may be self-hosting on
    # modest hardware (technical considerations: conservative defaults).
    if old_file and old_file.name != submission.file.name:
        old_file.delete(save=False)

    messages.success(request, "Submission received.")
    return redirect("core:homework")


# ---------------------------------------------------------------------
# Fee Notices — Phase 1 build sequence item 6, following the same
# admin/teacher-write, guardian-read shape Announcements (Section 23)
# and Homework (Section 24) established. Track 2 / private-school only
# (proposal Section 4.3) — informational only, no payment processing.
# Unlike Homework, a fee notice is always per-student (not per-class),
# since tuition/fees are owed by a specific family, not assigned to a
# whole class. Unlike Announcements, there's no draft/publish split —
# a fee notice is visible to that student's guardians as soon as it's
# saved — but it does have a status (unpaid/paid/waived, already on the
# model) that's changed via separate, explicit actions rather than a
# field on the edit form, the same reasoning Announcements' publish/
# unpublish split has: editing the amount and marking it paid are
# different enough actions that conflating them risks an accidental
# status change while fixing a typo.
# ---------------------------------------------------------------------

@login_required
def fee_notices_list(request):
    """Dispatches by role, same pattern as announcements_list/homework_list."""
    if request.school is None:
        messages.error(request, "You need to be linked to a school to see fee notices.")
        return redirect("core:dashboard")

    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        return _guardian_fee_notices(request)

    fee_notices = scope_to_school(
        FeeNotice.objects.select_related("student", "created_by"), request
    ).order_by("due_date")
    return render(request, "core/fee_notices_list.html", {"fee_notices": fee_notices})


def _guardian_fee_notices(request):
    fee_notices = (
        FeeNotice.objects.filter(
            school=request.school, student__guardian_links__guardian=request.user
        )
        .select_related("student")
        .distinct()
        .order_by("due_date")
    )
    return render(
        request, "core/guardian_fee_notices.html", {"fee_notices": fee_notices}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def fee_notice_new(request):
    if request.school is None:
        messages.error(
            request, "You need to be linked to a school before you can add a fee notice."
        )
        return redirect("core:dashboard")

    if request.method == "POST":
        form = FeeNoticeForm(request.POST, school=request.school)
        if form.is_valid():
            fee_notice = form.save(commit=False)
            fee_notice.created_by = request.user
            fee_notice.save()

            notify_users(
                fee_notice.student.guardians.all(),
                title="New fee notice",
                body=f"{fee_notice.title} — {fee_notice.student.first_name} {fee_notice.student.last_name}",
                url=reverse("core:fees"),
            )

            messages.success(
                request,
                f"“{fee_notice.title}” posted for {fee_notice.student.first_name} "
                f"{fee_notice.student.last_name} — visible to their guardians now.",
            )
            return redirect("core:fees")
    else:
        form = FeeNoticeForm(school=request.school)

    return render(request, "core/fee_notice_form.html", {"form": form})


def _get_fee_notice_or_404(request, pk):
    return get_object_or_404(scope_to_school(FeeNotice.objects.all(), request), pk=pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def fee_notice_edit(request, pk):
    fee_notice = _get_fee_notice_or_404(request, pk)

    if request.method == "POST":
        form = FeeNoticeForm(request.POST, instance=fee_notice, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(request, f"“{fee_notice.title}” updated.")
            return redirect("core:fees")
    else:
        form = FeeNoticeForm(instance=fee_notice, school=request.school)

    return render(
        request, "core/fee_notice_form.html", {"form": form, "fee_notice": fee_notice}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def fee_notice_delete(request, pk):
    """
    A real delete, same reasoning as announcement_delete/homework_delete:
    FeeNotice has no is_active field and nothing else references it, so
    there's no history a soft delete would be protecting.
    """
    fee_notice = _get_fee_notice_or_404(request, pk)
    title = fee_notice.title
    fee_notice.delete()
    messages.success(request, f"“{title}” deleted.")
    return redirect("core:fees")


def _set_fee_notice_status(request, pk, status, verb):
    fee_notice = _get_fee_notice_or_404(request, pk)
    fee_notice.status = status
    fee_notice.save(update_fields=["status"])
    messages.success(request, f"“{fee_notice.title}” marked {verb}.")
    return redirect("core:fees")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def fee_notice_mark_paid(request, pk):
    return _set_fee_notice_status(request, pk, FeeNotice.Status.PAID, "paid")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def fee_notice_mark_waived(request, pk):
    return _set_fee_notice_status(request, pk, FeeNotice.Status.WAIVED, "waived")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def fee_notice_mark_unpaid(request, pk):
    return _set_fee_notice_status(request, pk, FeeNotice.Status.UNPAID, "unpaid")


# ---------------------------------------------------------------------
# Permission Slips — Phase 1 build sequence item 7, the last of the
# four Communication sections. School-wide (school_class is null) or
# class-scoped, same audience pattern as Announcements/Fee Notices, but
# with real response tracking: creating or editing a slip generates one
# PermissionSlipResponse row per eligible student (active, with at
# least one linked guardian) via _sync_permission_slip_responses,
# defaulting to pending. A guardian responds Yes/No (with optional
# notes) for each of their own linked children — any guardian linked to
# that student can respond, not just whichever one the row happened to
# be pre-assigned to, since PermissionSlipResponse.guardian is a
# required field and something has to be there before anyone has
# actually responded (see the helper's docstring for the full
# reasoning).
# ---------------------------------------------------------------------

def _sync_permission_slip_responses(permission_slip):
    """
    Ensures a PermissionSlipResponse row exists for every student
    currently eligible for the given slip — active, in the slip's
    school, and (if the slip is class-scoped) in that class — and who
    has at least one linked guardian (no point tracking a response
    nobody can give). Idempotent: never touches a response that already
    exists, so re-running this after editing a slip only adds rows for
    newly-eligible students, it never resets or removes an existing
    (possibly already-answered) one.

    PermissionSlipResponse.guardian is a required foreign key — there's
    no "nobody has responded yet" null state on the model — so a new
    row is seeded with the student's primary-contact guardian (or, if
    none is marked primary, whichever guardian was linked first) purely
    as a placeholder assignee. Any guardian actually linked to that
    student can still respond (core.views.permission_slip_respond
    re-checks the GuardianLink fresh and overwrites `guardian` with
    whoever actually submitted the response), so this placeholder only
    affects who a "who hasn't responded yet" view would name before
    anyone has answered — it never restricts who's allowed to answer.
    """
    students = Student.objects.filter(school=permission_slip.school, is_active=True)
    if permission_slip.school_class_id:
        students = students.filter(school_class_id=permission_slip.school_class_id)
    students = students.filter(guardian_links__isnull=False).distinct()

    existing_student_ids = set(
        PermissionSlipResponse.objects.filter(
            permission_slip=permission_slip, student__in=students
        ).values_list("student_id", flat=True)
    )
    for student in students:
        if student.id in existing_student_ids:
            continue
        placeholder_link = (
            student.guardian_links.filter(is_primary_contact=True).first()
            or student.guardian_links.order_by("created_at").first()
        )
        PermissionSlipResponse.objects.create(
            permission_slip=permission_slip,
            student=student,
            guardian=placeholder_link.guardian,
            response=PermissionSlipResponse.Response.PENDING,
        )


@login_required
def permission_slips_list(request):
    """Dispatches by role, same pattern as announcements_list/homework_list/
    fee_notices_list."""
    if request.school is None:
        messages.error(request, "You need to be linked to a school to see permission slips.")
        return redirect("core:dashboard")

    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        return _guardian_permission_slips(request)

    slips = list(
        scope_to_school(
            PermissionSlip.objects.select_related("school_class", "created_by"), request
        )
        .prefetch_related("responses")
        .order_by("-created_at")
    )
    for slip in slips:
        responses = list(slip.responses.all())
        slip.response_total = len(responses)
        slip.response_yes = sum(
            1 for r in responses if r.response == PermissionSlipResponse.Response.YES
        )
        slip.response_no = sum(
            1 for r in responses if r.response == PermissionSlipResponse.Response.NO
        )
        slip.response_pending = sum(
            1 for r in responses if r.response == PermissionSlipResponse.Response.PENDING
        )
    return render(request, "core/permission_slips_list.html", {"permission_slips": slips})


def _guardian_permission_slips(request):
    """
    Builds one row per (permission slip, own child) pair the requesting
    guardian is entitled to respond to — flattened in the view rather
    than nested in the template, so a guardian isn't shown a school-wide
    slip with nothing under it just because they have no children
    linked yet (a real, if unusual, state — an orphaned guardian
    account, or one added before any GuardianLink existed).
    """
    class_ids = _relevant_class_ids_for_guardian(request)
    slips = list(
        PermissionSlip.objects.filter(school=request.school)
        .filter(Q(school_class__isnull=True) | Q(school_class_id__in=class_ids))
        .select_related("school_class")
        .order_by("response_deadline", "-created_at")
    )
    # Self-heal: a guardian linked to a student *after* a slip was
    # created wouldn't have a response row from the original sync — running
    # it again here (idempotent, see the helper's docstring) catches that
    # without requiring an admin to re-save the slip.
    for slip in slips:
        _sync_permission_slip_responses(slip)

    my_responses = (
        PermissionSlipResponse.objects.filter(
            permission_slip__in=slips, student__guardian_links__guardian=request.user
        )
        .select_related("student", "permission_slip__school_class")
        .order_by(
            "permission_slip__response_deadline",
            "student__last_name",
            "student__first_name",
        )
    )
    response_items = [
        {"permission_slip": response.permission_slip, "response": response}
        for response in my_responses
    ]

    return render(
        request, "core/guardian_permission_slips.html", {"response_items": response_items}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def permission_slip_new(request):
    if request.school is None:
        messages.error(
            request, "You need to be linked to a school before you can add a permission slip."
        )
        return redirect("core:dashboard")

    if request.method == "POST":
        form = PermissionSlipForm(request.POST, school=request.school)
        if form.is_valid():
            slip = form.save(commit=False)
            slip.created_by = request.user
            slip.save()
            _sync_permission_slip_responses(slip)

            eligible_students = Student.objects.filter(school=slip.school, is_active=True)
            if slip.school_class_id:
                eligible_students = eligible_students.filter(school_class_id=slip.school_class_id)
            notify_users(
                _guardian_users_for_students(eligible_students),
                title="New permission slip",
                body=slip.title,
                # core:permission_slips (the list dispatcher), not
                # permission_slip_detail — that per-student response
                # roster is admin/teacher-only, and a guardian tapping
                # this notification would otherwise land on a 403.
                url=reverse("core:permission_slips"),
            )

            messages.success(
                request,
                f"“{slip.title}” posted — visible to "
                f"{'that class' if slip.school_class else 'every'} student's guardians now.",
            )
            return redirect("core:permission_slips")
    else:
        form = PermissionSlipForm(school=request.school)

    return render(request, "core/permission_slip_form.html", {"form": form})


def _get_permission_slip_or_404(request, pk):
    return get_object_or_404(scope_to_school(PermissionSlip.objects.all(), request), pk=pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def permission_slip_detail(request, pk):
    """
    Single-slip view: every student's response, who (if anyone) has
    responded, and when — the "trackable guardian responses" the
    placeholder page promised. Re-runs _sync_permission_slip_responses
    first so a student added to the class (or newly given a guardian)
    since the slip was created shows up here too.
    """
    slip = _get_permission_slip_or_404(request, pk)
    _sync_permission_slip_responses(slip)
    responses = slip.responses.select_related("student", "guardian").order_by(
        "student__last_name", "student__first_name"
    )
    return render(
        request,
        "core/permission_slip_detail.html",
        {"permission_slip": slip, "responses": responses},
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def permission_slip_edit(request, pk):
    slip = _get_permission_slip_or_404(request, pk)

    if request.method == "POST":
        form = PermissionSlipForm(request.POST, instance=slip, school=request.school)
        if form.is_valid():
            form.save()
            _sync_permission_slip_responses(slip)
            messages.success(request, f"“{slip.title}” updated.")
            return redirect("core:permission_slips")
    else:
        form = PermissionSlipForm(instance=slip, school=request.school)

    return render(
        request, "core/permission_slip_form.html", {"form": form, "permission_slip": slip}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def permission_slip_delete(request, pk):
    """
    A real delete, same reasoning as announcement_delete/homework_delete/
    fee_notice_delete: PermissionSlip has no is_active field, and
    deleting one cascades to its PermissionSlipResponse rows — response
    records for a slip that no longer exists have no reason to stick
    around.
    """
    slip = _get_permission_slip_or_404(request, pk)
    title = slip.title
    slip.delete()
    messages.success(request, f"“{title}” deleted.")
    return redirect("core:permission_slips")


@login_required
@role_required(SchoolMembership.Role.GUARDIAN)
@require_POST
def permission_slip_respond(request, pk, student_pk):
    """
    Records a guardian's Yes/No response (with optional notes) for one
    of their own linked children. Re-verifies the guardian-student link
    fresh via GuardianLink — not just that a PermissionSlipResponse row
    with this student_pk exists — same reasoning as
    announcement_mark_read: a guardian shouldn't be able to respond on
    behalf of an unrelated student by guessing ids in a POST, even
    though the row's pre-seeded `guardian` field might name someone
    else entirely (see _sync_permission_slip_responses's docstring).
    """
    slip = get_object_or_404(PermissionSlip.objects.filter(school=request.school), pk=pk)
    if not GuardianLink.objects.filter(guardian=request.user, student_id=student_pk).exists():
        raise Http404

    response_value = request.POST.get("response")
    valid_responses = {
        PermissionSlipResponse.Response.YES,
        PermissionSlipResponse.Response.NO,
    }
    if response_value not in valid_responses:
        messages.error(request, "Choose Yes or No to respond.")
        return redirect("core:permission_slips")

    response, _created = PermissionSlipResponse.objects.get_or_create(
        permission_slip=slip,
        student_id=student_pk,
        defaults={"guardian": request.user, "response": PermissionSlipResponse.Response.PENDING},
    )
    response.response = response_value
    response.guardian = request.user
    response.notes = request.POST.get("notes", "")
    response.responded_at = timezone.now()
    response.save(update_fields=["response", "guardian", "notes", "responded_at"])
    messages.success(request, "Your response has been recorded.")
    return redirect("core:permission_slips")


# ---------------------------------------------------------------------
# School Calendar — a per-school list of closed days (public holidays,
# in-service days, school-declared breaks). Deliberately narrower than
# every other section above in one specific way: write access is
# admin-only, not admin+teacher — "an admin maintains a list," with
# teachers getting the same read-only view guardians get, since only
# one person per school should be actively curating this. It's a
# calendar of exceptions, not a scheduling system: no timetables, no
# recurring events, just which days the school is closed.
#
# The other half of this feature — the soft, non-blocking warning that
# shows up on the homework/fee-notice/permission-slip due-date pickers
# — is calendar_closed_days_json below, plus the small shared script in
# core/static/core/js/calendar-warnings.js those three form templates
# include.
# ---------------------------------------------------------------------

@login_required
def calendar_list(request):
    """Dispatches by role: an admin gets the full management list (past
    and future, with add/edit/delete), teachers and guardians get a
    read-only list of upcoming closed days only — same read-only shape
    for both, since neither can edit this calendar."""
    if request.school is None:
        messages.error(request, "You need to be linked to a school to see the calendar.")
        return redirect("core:dashboard")

    if request.membership.role != SchoolMembership.Role.ADMIN:
        events = scope_to_school(
            SchoolCalendarEvent.objects.filter(end_date__gte=timezone.localdate()), request
        ).order_by("start_date")
        return render(request, "core/calendar_readonly.html", {"events": events})

    events = scope_to_school(SchoolCalendarEvent.objects.all(), request).order_by("start_date")
    return render(request, "core/calendar_list.html", {"events": events})


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def calendar_new(request):
    if request.school is None:
        messages.error(
            request, "You need to be linked to a school before you can add a closed day."
        )
        return redirect("core:dashboard")

    if request.method == "POST":
        form = SchoolCalendarEventForm(request.POST, school=request.school)
        if form.is_valid():
            event = form.save(commit=False)
            event.created_by = request.user
            event.save()
            messages.success(request, f"“{event.label}” added to the calendar.")
            return redirect("core:calendar")
    else:
        form = SchoolCalendarEventForm(school=request.school)

    return render(request, "core/calendar_form.html", {"form": form})


def _get_calendar_event_or_404(request, pk):
    return get_object_or_404(
        scope_to_school(SchoolCalendarEvent.objects.all(), request), pk=pk
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def calendar_edit(request, pk):
    event = _get_calendar_event_or_404(request, pk)

    if request.method == "POST":
        form = SchoolCalendarEventForm(request.POST, instance=event, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(request, f"“{event.label}” updated.")
            return redirect("core:calendar")
    else:
        form = SchoolCalendarEventForm(instance=event, school=request.school)

    return render(request, "core/calendar_form.html", {"form": form, "event": event})


@login_required
@role_required(SchoolMembership.Role.ADMIN)
@require_POST
def calendar_delete(request, pk):
    event = _get_calendar_event_or_404(request, pk)
    label = event.label
    event.delete()
    messages.success(request, f"“{label}” removed from the calendar.")
    return redirect("core:calendar")


@login_required
def calendar_closed_days_json(request):
    """
    Feeds the non-blocking due-date warning script (core/static/core/js/
    calendar-warnings.js) used on the homework, fee notice, and
    permission slip forms. Any logged-in role can fetch this — the
    calendar itself is already visible read-only to teachers and
    guardians, so there's nothing here a JSON copy of the same rows
    exposes that the calendar page doesn't already show — but in
    practice only admins/teachers filling in a due-date field ever
    actually call it, since guardians never see those forms.

    Scoped to request.school like everything else; returns an empty
    list rather than an error if there's no active school, since a
    missing/empty warning is a safe default for a script that isn't
    essential to the form it's enhancing.
    """
    if request.school is None:
        return JsonResponse({"closed_days": []})

    events = scope_to_school(SchoolCalendarEvent.objects.all(), request)
    return JsonResponse(
        {
            "closed_days": [
                {
                    "label": event.label,
                    "start": event.start_date.isoformat(),
                    "end": event.end_date.isoformat(),
                    "type": event.event_type,
                }
                for event in events
            ]
        }
    )


# ---------------------------------------------------------------------
# Attendance — a daily present/absent/late record per student per class
# (roadmap: Attendance Tracking). Deliberately a simple daily roster,
# not a period-by-period tracker: Notipa has no timetable concept, and
# this section doesn't introduce one. Any teacher/admin at the school
# can take or view any class's roster — the same deliberately
# permissive "any teacher can see any class" posture class_detail
# already uses, rather than restricting each teacher to only their own
# homeroom. A guardian only ever reaches their own child's history
# (my_child_attendance), never a class-level view — see that view's
# docstring for how that's enforced.
# ---------------------------------------------------------------------

def _can_edit_attendance(request, record_date):
    """
    Same-day edits are unrestricted for any teacher/admin at the
    school; edits to an older date require the requesting user to be a
    school admin. This is the "editable within a window" rule from the
    roadmap: a teacher can fix a same-day mistake (a student arrived
    late and was marked absent) freely, but the historical record
    beyond today needs an admin to amend, so it isn't casually rewritten
    after the fact.
    """
    if record_date >= timezone.localdate():
        return True
    return request.membership is not None and request.membership.role == SchoolMembership.Role.ADMIN


def _closed_day_for(school, on_date):
    """The SchoolCalendarEvent covering on_date at this school, if any —
    used to show a "school is closed" note on the roster (roadmap:
    "closed-day awareness ... skip holidays automatically") rather than
    to block anything: a school can still record something for a
    closed day if it genuinely needs to, this is just a heads-up."""
    return SchoolCalendarEvent.objects.filter(
        school=school, start_date__lte=on_date, end_date__gte=on_date
    ).first()


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def attendance_classes_list(request):
    """
    Landing page for Attendance: every active class at the school, each
    flagged with whether today's roster has already been taken — the
    roadmap's "visible 'not yet taken today' indicator" at the
    all-classes level, for a teacher or admin who teaches/oversees more
    than one class. class_detail shows the same indicator for a single
    class already viewed.
    """
    today = timezone.localdate()
    classes = list(
        scope_to_school(SchoolClass.objects.filter(is_active=True), request).order_by("name")
    )
    taken_class_ids = set(
        AttendanceRecord.objects.filter(school_class__in=classes, date=today)
        .values_list("school_class_id", flat=True)
        .distinct()
    )
    for school_class in classes:
        school_class.attendance_taken_today = school_class.id in taken_class_ids

    return render(
        request, "core/attendance_classes_list.html", {"classes": classes, "today": today}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def attendance_roster(request, pk):
    """
    One-screen daily roster for a single class/day: every active
    student defaults to present, with a quick control to mark absent or
    late instead — "a fast checklist, not a form per student" (roadmap).
    Saving upserts one AttendanceRecord per student for the date (the
    model's unique constraint), so retaking an already-taken day updates
    it in place rather than creating duplicates.

    Date is driven by a ?date= query param plus prev/next-day links
    rather than a separate page per day, and defaults to today
    (school-local). A future date isn't allowed — attendance is taken
    for a day that's happening or has happened, not in advance.
    Whether the date is actually editable by *this* request is decided
    by _can_edit_attendance; if not, the roster still renders read-only
    rather than 404ing, so a teacher can still look back at an older day
    even if only an admin can change it.
    """
    school_class = _get_class_or_404(request, pk)
    today = timezone.localdate()

    date_param = request.GET.get("date")
    try:
        roster_date = date.fromisoformat(date_param) if date_param else today
    except ValueError:
        roster_date = today
    if roster_date > today:
        messages.error(request, "You can't take attendance for a future date.")
        return redirect("core:attendance_roster", pk=pk)

    can_edit = _can_edit_attendance(request, roster_date)
    closed_event = _closed_day_for(school_class.school, roster_date)

    students = list(school_class.students.filter(is_active=True).order_by("last_name", "first_name"))
    existing_by_student_id = {
        r.student_id: r
        for r in AttendanceRecord.objects.filter(school_class=school_class, date=roster_date)
    }

    if request.method == "POST":
        if not can_edit:
            return render(request, "core/no_access.html", status=403)
        for student in students:
            status = request.POST.get(f"status-{student.id}")
            if status not in AttendanceRecord.Status.values:
                continue
            AttendanceRecord.objects.update_or_create(
                student=student,
                date=roster_date,
                defaults={
                    "school_class": school_class,
                    "status": status,
                    "recorded_by": request.user,
                },
            )
        messages.success(request, f"Attendance saved for {roster_date:%B %-d, %Y}.")
        return redirect(f"{reverse('core:attendance_roster', args=[pk])}?date={roster_date.isoformat()}")

    roster = [
        {
            "student": student,
            "status": existing_by_student_id[student.id].status
            if student.id in existing_by_student_id
            else AttendanceRecord.Status.PRESENT,
        }
        for student in students
    ]

    return render(
        request,
        "core/attendance_roster.html",
        {
            "school_class": school_class,
            "roster": roster,
            "roster_date": roster_date,
            "today": today,
            "can_edit": can_edit,
            "closed_event": closed_event,
            "taken": bool(existing_by_student_id),
            "prev_date": roster_date - timedelta(days=1),
            "next_date": roster_date + timedelta(days=1) if roster_date < today else None,
            "status_choices": AttendanceRecord.Status.choices,
        },
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def attendance_overview(request):
    """
    School-wide attendance snapshot for an admin who needs the bigger
    picture beyond one class's daily roster — the roadmap's "cases
    where an admin needs the bigger picture (e.g. flagging a pattern of
    absences)". Present/absent/late totals per class over the last 30
    days; admin-only, since this is the one view that shows every
    class's numbers side by side rather than a single family's or a
    single class's own roster.
    """
    today = timezone.localdate()
    window_start = today - timedelta(days=30)
    classes = list(
        scope_to_school(SchoolClass.objects.filter(is_active=True), request).order_by("name")
    )
    counts_by_class_id = {}
    records = AttendanceRecord.objects.filter(
        school_class__in=classes, date__gte=window_start, date__lte=today
    ).values("school_class_id", "status")
    for row in records:
        bucket = counts_by_class_id.setdefault(
            row["school_class_id"], {"present": 0, "absent": 0, "late": 0}
        )
        bucket[row["status"]] += 1

    rows = []
    for school_class in classes:
        counts = counts_by_class_id.get(school_class.id, {"present": 0, "absent": 0, "late": 0})
        rows.append(
            {
                "school_class": school_class,
                "present": counts["present"],
                "absent": counts["absent"],
                "late": counts["late"],
                "total": sum(counts.values()),
            }
        )

    return render(
        request,
        "core/attendance_overview.html",
        {"rows": rows, "window_start": window_start, "today": today},
    )


@login_required
@role_required(SchoolMembership.Role.GUARDIAN)
def my_child_attendance(request, pk):
    """
    A guardian's own child's attendance history — never another
    student's, even another child in the same class (roadmap: "visible
    to each student's own linked guardians ... but never the rest of
    the class's records"). Scoped the same two ways as my_child_detail:
    role_required(GUARDIAN) restricts this to guardian accounts, and the
    GuardianLink lookup below restricts it further to a student this
    guardian is actually linked to — a guardian can't view another
    family's child's attendance by guessing a student id, and there is
    no class-level or school-level route a guardian can reach at all
    (attendance_roster/attendance_classes_list/attendance_overview are
    all role_required(ADMIN, TEACHER) or ADMIN-only).
    """
    link = get_object_or_404(
        GuardianLink.objects.select_related("student__school_class").filter(
            student__school=request.school
        ),
        guardian=request.user,
        student_id=pk,
    )
    student = link.student
    records = list(AttendanceRecord.objects.filter(student=student).order_by("-date")[:90])
    summary = {"present": 0, "absent": 0, "late": 0}
    for record in records:
        summary[record.status] += 1

    return render(
        request,
        "core/my_child_attendance.html",
        {"student": student, "records": records, "summary": summary},
    )


# ---------------------------------------------------------------------
# Report Cards — a term-by-term academic report per student (roadmap:
# "Report Cards per Student"), replacing "the photocopied slip sent
# home in a backpack" rather than becoming a gradebook. Two pieces:
# Term (admin-managed, school-wide) and ReportCard/ReportCardEntry
# (teacher-authored, one per student per term, draft-then-published
# the same way Announcements are). A teacher is scoped to their own
# class(es) here — unlike Attendance's deliberately permissive "any
# teacher can see any class" — since report-card content is graded
# academic judgement about a specific student, not a daily checklist;
# an admin can still do it "on a teacher's behalf" per the roadmap, so
# admin access isn't restricted the same way.
# ---------------------------------------------------------------------

def _classes_taught_by(request):
    """
    The SchoolClass queryset this request is allowed to enter report
    cards for: every active class at the school for an admin ("on a
    teacher's behalf"), or only the classes a teacher actually teaches
    (homeroom or co-teacher) for anyone else. Mirrors
    core.messaging._teacher_ids_for_class in reverse — that helper
    finds the teachers for a class, this finds the classes for a
    teacher.
    """
    classes = scope_to_school(SchoolClass.objects.filter(is_active=True), request)
    if request.membership.role == SchoolMembership.Role.ADMIN:
        return classes
    return classes.filter(
        Q(homeroom_teacher__user=request.user) | Q(additional_teachers__user=request.user)
    ).distinct()


def _get_taught_class_or_404(request, pk):
    return get_object_or_404(_classes_taught_by(request), pk=pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def term_list(request):
    """Admin-only term management screen — the roadmap's "Term
    management screen for admins." No read-only view for teachers/
    guardians the way the school calendar has one: a term only matters
    to them as a dropdown on the report-card screens, not as its own
    page to browse."""
    terms = scope_to_school(Term.objects.all(), request).order_by("-start_date")
    return render(request, "core/term_list.html", {"terms": terms})


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def term_new(request):
    if request.school is None:
        messages.error(request, "You need to be linked to a school before you can add a term.")
        return redirect("core:dashboard")

    if request.method == "POST":
        form = TermForm(request.POST, school=request.school)
        if form.is_valid():
            term = form.save()
            messages.success(request, f"“{term.name}” added.")
            return redirect("core:terms")
    else:
        form = TermForm(school=request.school)

    return render(request, "core/term_form.html", {"form": form})


def _get_term_or_404(request, pk):
    return get_object_or_404(scope_to_school(Term.objects.all(), request), pk=pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def term_edit(request, pk):
    term = _get_term_or_404(request, pk)

    if request.method == "POST":
        form = TermForm(request.POST, instance=term, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(request, f"“{term.name}” updated.")
            return redirect("core:terms")
    else:
        form = TermForm(instance=term, school=request.school)

    return render(request, "core/term_form.html", {"form": form, "term": term})


@login_required
@role_required(SchoolMembership.Role.ADMIN)
@require_POST
def term_delete(request, pk):
    term = _get_term_or_404(request, pk)
    name = term.name
    term.delete()
    messages.success(request, f"“{name}” deleted.")
    return redirect("core:terms")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def report_cards_classes_list(request):
    """Landing page for Report Cards: every class this user can enter
    reports for (core._classes_taught_by), same "pick a class" shape as
    Attendance's equivalent landing page."""
    classes = list(_classes_taught_by(request).order_by("name"))
    return render(request, "core/report_cards_classes_list.html", {"classes": classes})


def _attendance_summary_for_term(student, term):
    """Present/absent/late counts for a student across a term's date
    range — the roadmap's "attendance summary ... pulled in
    automatically once attendance tracking exists." Computed on the fly
    from core.models.AttendanceRecord, never re-entered by hand and
    never stored on the report itself, so it always reflects whatever
    attendance has actually been recorded up to the moment the report
    is viewed."""
    counts = {"present": 0, "absent": 0, "late": 0}
    rows = AttendanceRecord.objects.filter(
        student=student, date__gte=term.start_date, date__lte=term.end_date
    ).values_list("status", flat=True)
    for status in rows:
        counts[status] += 1
    return counts


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def report_cards_roster(request, pk):
    """
    One class's report-card roster for a selected term (?term=): every
    active student, with their report's current status (none / draft /
    published) and a link into report_card_edit. Term defaults to the
    school's most recently started term; if the school has no terms
    yet, this points admins at term_new instead of showing an empty
    roster (there's genuinely nothing to enter until a term exists).
    """
    school_class = _get_taught_class_or_404(request, pk)
    terms = list(scope_to_school(Term.objects.all(), request).order_by("-start_date"))

    if not terms:
        return render(
            request, "core/report_cards_roster.html",
            {"school_class": school_class, "terms": terms, "term": None},
        )

    term_id = request.GET.get("term")
    term = next((t for t in terms if str(t.id) == term_id), None) or terms[0]

    students = list(school_class.students.filter(is_active=True).order_by("last_name", "first_name"))
    reports_by_student_id = {
        r.student_id: r
        for r in ReportCard.objects.filter(term=term, student__in=students)
    }
    roster = [
        {"student": s, "report": reports_by_student_id.get(s.id)} for s in students
    ]
    draft_count = sum(1 for row in roster if row["report"] and row["report"].status == ReportCard.Status.DRAFT)

    return render(
        request,
        "core/report_cards_roster.html",
        {
            "school_class": school_class,
            "terms": terms,
            "term": term,
            "roster": roster,
            "draft_count": draft_count,
        },
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def report_cards_publish_all(request, pk, term_pk):
    """Bulk "publish all drafts for this term" — the roadmap's own
    phrasing, since a teacher will typically finish a whole class at
    once rather than publishing report by report."""
    school_class = _get_taught_class_or_404(request, pk)
    term = _get_term_or_404(request, term_pk)
    draft_reports = list(
        ReportCard.objects.filter(
            school_class=school_class, term=term, status=ReportCard.Status.DRAFT
        ).select_related("student")
    )
    updated = ReportCard.objects.filter(
        pk__in=[r.pk for r in draft_reports]
    ).update(status=ReportCard.Status.PUBLISHED, published_at=timezone.now())
    for report in draft_reports:
        notify_users(
            report.student.guardians.all(),
            title="New report card",
            body=f"{term.name} — {report.student.first_name} {report.student.last_name}",
            url=reverse("core:my_child_report_cards", args=[report.student.pk]),
        )
    if updated:
        messages.success(request, f"Published {updated} report{'s' if updated != 1 else ''} for {term.name}.")
    else:
        messages.error(request, "No draft reports to publish for this term.")
    return redirect(f"{reverse('core:report_cards_roster', args=[pk])}?term={term.pk}")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def report_card_edit(request, pk, term_pk, student_pk):
    """
    Create-or-edit a single student's report for a term: a comment
    field (ReportCardForm) plus a formset of subject/grade rows
    (ReportCardEntryFormSet) — "a configurable set of subject/grade
    rows" the roadmap describes, not a fixed schema. Two submit
    buttons, "save_draft" and "publish" (checked in POST), decide
    whether this leaves the report as a draft or makes it visible to
    the student's guardians; there's no form field for status a
    teacher could flip by accident while editing the comment.

    On first creating a report (no entries yet) for this class/term,
    the subject rows are pre-filled by copying subject names (blank
    grades) from any sibling report already started for the same
    class/term — the roadmap's "ability to duplicate the subject list
    across students in a class to speed up entry" — without needing a
    separate "copy from" control.
    """
    school_class = _get_taught_class_or_404(request, pk)
    term = _get_term_or_404(request, term_pk)
    student = get_object_or_404(school_class.students.filter(is_active=True), pk=student_pk)

    report, created = ReportCard.objects.get_or_create(
        student=student, term=term,
        defaults={"school_class": school_class, "created_by": request.user},
    )

    initial_entries = None
    if created and not report.entries.exists():
        sibling = (
            ReportCard.objects.filter(school_class=school_class, term=term)
            .exclude(pk=report.pk)
            .prefetch_related("entries")
            .first()
        )
        if sibling and sibling.entries.exists():
            initial_entries = [
                {"subject": e.subject, "grade": "", "order": e.order}
                for e in sibling.entries.order_by("order", "subject")
            ]

    if request.method == "POST":
        # extra doesn't matter for a bound (POST) formset — the actual
        # number of forms to parse comes from the submitted TOTAL_FORMS
        # management field, not this factory argument — so the plain
        # default is fine here regardless of what was shown on the GET
        # that produced this submission.
        formset_class = report_card_entry_formset_factory()
        form = ReportCardForm(request.POST, instance=report)
        formset = formset_class(request.POST, instance=report)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            report.school_class = school_class
            report.created_by = request.user
            if "publish" in request.POST:
                report.status = ReportCard.Status.PUBLISHED
                if not report.published_at:
                    report.published_at = timezone.now()
                report.save()

                notify_users(
                    student.guardians.all(),
                    title="New report card",
                    body=f"{term.name} — {student.first_name} {student.last_name}",
                    url=reverse("core:my_child_report_cards", args=[student.pk]),
                )

                messages.success(
                    request, f"Report for {student.first_name} {student.last_name} published."
                )
            else:
                report.status = ReportCard.Status.DRAFT
                report.save()
                messages.success(
                    request, f"Draft saved for {student.first_name} {student.last_name}."
                )
            return redirect(f"{reverse('core:report_cards_roster', args=[pk])}?term={term.pk}")
    else:
        form = ReportCardForm(instance=report)
        # How many blank rows to start with: exactly enough to hold a
        # copied sibling subject list, a handful if this report already
        # has its own saved entries (the "+ Add Subject Row" button
        # covers anything more — no need to pad the page with rows that
        # would just reappear blank, unsaved, every time this page is
        # reopened), or a more generous starting count for a genuinely
        # new report with nothing entered and no sibling to copy from.
        if initial_entries:
            extra = len(initial_entries)
        elif report.entries.exists():
            extra = 3
        else:
            extra = 8
        formset_class = report_card_entry_formset_factory(extra=extra)
        formset = formset_class(
            instance=report, initial=initial_entries if initial_entries else None
        )

    attendance_summary = _attendance_summary_for_term(student, term)

    return render(
        request,
        "core/report_card_form.html",
        {
            "school_class": school_class,
            "term": term,
            "student": student,
            "report": report,
            "form": form,
            "formset": formset,
            "attendance_summary": attendance_summary,
        },
    )


def _can_view_report_card(request, report):
    """A report card is viewable by: an admin/teacher who can enter
    reports for its class (_classes_taught_by), or a guardian actually
    linked to its student — and then only once it's published. Shared
    by report_card_view and my_child_report_cards' link-building, so
    there's exactly one place this rule lives."""
    if request.membership.role in (SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER):
        return _classes_taught_by(request).filter(pk=report.school_class_id).exists()
    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        return report.status == ReportCard.Status.PUBLISHED and GuardianLink.objects.filter(
            guardian=request.user, student=report.student
        ).exists()
    return False


@login_required
def report_card_view(request, pk):
    """
    Print-friendly single-report page (core/report_card_print.html —
    clean CSS, no app chrome, the roadmap's "guardian can save or print
    a copy"). Reachable by a teacher/admin who could have entered this
    report, or by one of the student's own linked guardians once it's
    published — never a guardian browsing by guessing a report id for
    an unrelated (or still-draft) student, which is exactly what
    _can_view_report_card checks. Viewing as a guardian records a
    ReportCardRead, the read-tracking the roadmap's Overview mentions.
    """
    report = get_object_or_404(
        ReportCard.objects.select_related("student", "term", "school_class").prefetch_related("entries"),
        pk=pk,
    )
    if request.school is None or report.school_class.school_id != request.school.id:
        return render(request, "core/no_access.html", status=403)
    if not _can_view_report_card(request, report):
        return render(request, "core/no_access.html", status=403)

    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        ReportCardRead.objects.get_or_create(report_card=report, guardian=request.user)

    attendance_summary = _attendance_summary_for_term(report.student, report.term)
    return render(
        request,
        "core/report_card_print.html",
        {
            "report": report,
            "student": report.student,
            "term": report.term,
            "entries": report.entries.all(),
            "attendance_summary": attendance_summary,
        },
    )


@login_required
@role_required(SchoolMembership.Role.GUARDIAN)
def my_child_report_cards(request, pk):
    """
    A guardian's own child's published report cards, newest term
    first — the roadmap's "Report card view on a student's profile,
    showing published reports by term, newest first." Scoped the same
    two ways as my_child_attendance: role_required(GUARDIAN), plus the
    GuardianLink lookup below restricting this to a student the
    guardian is actually linked to.
    """
    link = get_object_or_404(
        GuardianLink.objects.filter(student__school=request.school),
        guardian=request.user,
        student_id=pk,
    )
    student = link.student
    reports = (
        ReportCard.objects.filter(student=student, status=ReportCard.Status.PUBLISHED)
        .select_related("term")
        .order_by("-term__start_date")
    )
    return render(
        request, "core/my_child_report_cards.html", {"student": student, "reports": reports}
    )


# ---------------------------------------------------------------------
# Messaging — a private, one-to-one thread between a guardian and their
# child's teacher, scoped to the shared student. Off by default per
# school (School.messaging_enabled, set from Admin > Settings) and,
# underneath that, opt-out per class (SchoolClass.messaging_enabled —
# proposal: "Per-School Messaging On/Off Switch"). Every thread-creating
# and thread-posting view below re-checks core.messaging.
# class_messaging_effectively_enabled / thread_messaging_enabled
# server-side rather than only hiding the entry points in a template,
# the same "never just hidden in the UI" posture the rest of this app's
# permission layer uses. Viewing an already-existing thread is
# deliberately *not* gated on either switch — turning messaging off
# doesn't delete history, only stops new threads/messages (see
# message_thread_detail's docstring).
#
# A thread can only ever be started between a guardian actually linked
# to a student (GuardianLink) and a teacher actually teaching that
# student's class (SchoolClass.homeroom_teacher or additional_teachers)
# — never an open directory of arbitrary staff. That connection is
# re-verified fresh from the data on every thread-start attempt, not
# trusted from whatever ids happen to be in the URL.
# ---------------------------------------------------------------------

def _connected_teacher_ids_for_student(student):
    """User ids of every teacher (homeroom + co-teachers) actually
    teaching this student's current class — the set a guardian is
    allowed to start a message thread with about this student. Empty
    if the student has no class assigned yet."""
    if not student.school_class_id:
        return set()
    school_class = student.school_class
    ids = set(school_class.additional_teachers.values_list("user_id", flat=True))
    if school_class.homeroom_teacher_id:
        ids.add(school_class.homeroom_teacher.user_id)
    return ids


def _connected_guardian_ids_for_student(student):
    """User ids of every guardian actually linked to this student — the
    set a teacher is allowed to start a message thread with about this
    student."""
    return set(student.guardian_links.values_list("guardian_id", flat=True))


@login_required
def messages_inbox(request):
    """
    Lists the requesting user's own message threads — guardian or
    teacher, one-to-one or class group, same template either way —
    ordered by most recent activity, each annotated with an "other
    party" label and an unread count. Neither role ever sees another
    person's threads or another class's group thread: one-to-one
    threads are filtered to guardian=request.user or teacher=
    request.user exactly as before, and class threads are filtered to
    participants=request.user, which core.messaging.sync_class_thread
    is what actually keeps trustworthy — never "every thread at this
    school."

    Unread counting differs by thread type because read-tracking does:
    a one-to-one thread's Message.read_at only has room for one possible
    "other reader" (see that field's docstring), so it's used directly;
    a class thread can have many readers, so it compares each message's
    sent_at against this user's own MessageThreadRead.last_read_at
    instead. A removed message never counts as unread either way — there's
    nothing new to read in a placeholder.

    Deliberately not gated on request.school.messaging_enabled (unlike
    message_thread_start/message_thread_detail's composer): this is the
    inbox, the one place "existing threads are preserved, not deleted,
    but become read-only" (proposal: "Per-School Messaging On/Off
    Switch") is actually meant to be seen. Turning messaging off hides
    the sidebar link to this page (templates/base.html) and blocks new
    threads/messages, but doesn't take away a user's ability to read
    their own history if they land here directly.
    """
    if request.school is None:
        messages.error(request, "You need to be linked to a school to see messages.")
        return redirect("core:dashboard")
    if request.membership.role not in (
        SchoolMembership.Role.GUARDIAN,
        SchoolMembership.Role.TEACHER,
    ):
        return render(request, "core/no_access.html", status=403)

    is_guardian = request.membership.role == SchoolMembership.Role.GUARDIAN
    one_to_one_kwargs = {"guardian": request.user} if is_guardian else {"teacher": request.user}
    threads = list(
        MessageThread.objects.filter(school=request.school)
        .filter(
            Q(thread_type=MessageThread.ThreadType.ONE_TO_ONE, **one_to_one_kwargs)
            | Q(thread_type=MessageThread.ThreadType.CLASS, participants=request.user)
        )
        .select_related("student", "guardian", "teacher", "school_class")
        .prefetch_related("messages")
        .distinct()
        .order_by("-last_message_at", "-created_at")
    )
    read_state_by_thread_id = {
        rs.thread_id: rs.last_read_at
        for rs in MessageThreadRead.objects.filter(thread__in=threads, user=request.user)
    }
    for thread in threads:
        thread_messages = list(thread.messages.all())
        thread.last_message = max(thread_messages, key=lambda m: m.sent_at, default=None)
        if thread.thread_type == MessageThread.ThreadType.CLASS:
            thread.other_party = None
            last_read_at = read_state_by_thread_id.get(thread.id)
            thread.unread_count = sum(
                1
                for m in thread_messages
                if m.sender_id != request.user.id
                and not m.removed_at
                and (last_read_at is None or m.sent_at > last_read_at)
            )
        else:
            thread.other_party = thread.teacher if is_guardian else thread.guardian
            thread.unread_count = sum(
                1 for m in thread_messages if m.sender_id != request.user.id and m.read_at is None
            )

    return render(
        request,
        "core/messages_inbox.html",
        {"threads": threads, "messaging_enabled": request.school.messaging_enabled},
    )


@login_required
def message_thread_start(request, student_pk, other_user_pk):
    """
    Creates (or reuses) the thread between request.user and
    other_user_pk about student_pk, then redirects into it. Re-verifies
    the guardian-teacher-student connection fresh from GuardianLink/
    SchoolClass every time — same "never trust an id from outside the
    session/data relationships" posture as permission_slip_respond and
    homework_submit — rather than trusting that a pair of ids in the
    URL is one this user was actually shown a "Message" button for.

    Also re-checks class_messaging_effectively_enabled for the student's
    class server-side, not just the school flag — a disabled school or a
    class that's opted out both block *new* one-to-one threads the same
    way they block new class threads, even though an existing thread
    between the same two people stays readable (core.views.
    message_thread_detail doesn't 404 on this).
    """
    if request.school is None:
        raise Http404
    if request.membership.role not in (
        SchoolMembership.Role.GUARDIAN,
        SchoolMembership.Role.TEACHER,
    ):
        raise Http404

    student = get_object_or_404(Student.objects.filter(school=request.school), pk=student_pk)
    if not class_messaging_effectively_enabled(student.school_class):
        raise Http404

    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        guardian_id = request.user.id
        teacher_id = other_user_pk
        if guardian_id not in _connected_guardian_ids_for_student(student):
            raise Http404
        if teacher_id not in _connected_teacher_ids_for_student(student):
            raise Http404
    else:
        teacher_id = request.user.id
        guardian_id = other_user_pk
        if teacher_id not in _connected_teacher_ids_for_student(student):
            raise Http404
        if guardian_id not in _connected_guardian_ids_for_student(student):
            raise Http404

    thread, _created = MessageThread.objects.get_or_create(
        school=request.school,
        student=student,
        guardian_id=guardian_id,
        teacher_id=teacher_id,
    )
    return redirect("core:message_thread_detail", pk=thread.pk)


def _get_message_thread_or_404(request, pk):
    """
    Scoped two ways for both thread types: request.membership.role
    decides which side of a one-to-one thread request.user has to be on
    (guardian= or teacher=), and for a class thread, participants=
    request.user is what stands in for that — core.messaging.
    sync_class_thread is what keeps that participants list trustworthy
    (added the moment a guardian/teacher connects to the class, removed
    the moment they don't), so this can't be reached for a class this
    user was never — or is no longer — part of, even by guessing a pk.
    """
    if request.membership is None:
        raise Http404
    if request.membership.role == SchoolMembership.Role.GUARDIAN:
        qs = MessageThread.objects.filter(school=request.school).filter(
            Q(thread_type=MessageThread.ThreadType.ONE_TO_ONE, guardian=request.user)
            | Q(thread_type=MessageThread.ThreadType.CLASS, participants=request.user)
        )
    elif request.membership.role == SchoolMembership.Role.TEACHER:
        qs = MessageThread.objects.filter(school=request.school).filter(
            Q(thread_type=MessageThread.ThreadType.ONE_TO_ONE, teacher=request.user)
            | Q(thread_type=MessageThread.ThreadType.CLASS, participants=request.user)
        )
    else:
        qs = MessageThread.objects.none()
    return get_object_or_404(
        qs.select_related("student", "guardian", "teacher", "school_class").distinct(), pk=pk
    )


@login_required
def message_thread_detail(request, pk):
    """
    Shows one thread's full message history plus a composer (unless a
    class thread's announcements_only is on and this user is a guardian,
    in which case the composer is hidden — enforced here, not just in
    the template, same "never trust the UI alone" posture as everywhere
    else access is decided), and records that this user has seen the
    thread up to now.

    Read-tracking branches by thread type: a one-to-one thread marks any
    message sent by the other participant as read directly on the
    message (Message.read_at — see its docstring for why that only
    works with exactly two possible participants); a class thread
    instead upserts this user's own MessageThreadRead row, since there's
    no single "the other reader" to stamp a message with once a thread
    can have many.

    Viewing is deliberately not gated on the messaging on/off switches
    at all (proposal: "Per-School Messaging On/Off Switch" — "existing
    threads are preserved, not deleted, but become read-only"): a
    participant can always open a thread they're already in, only the
    composer is conditional. thread_messaging_enabled decides whether
    the switches themselves currently allow posting; can_post folds that
    together with the pre-existing announcements_only rule, so the
    template only has to check one flag either way.
    """
    if request.school is None:
        raise Http404
    thread = _get_message_thread_or_404(request, pk)
    is_class = thread.thread_type == MessageThread.ThreadType.CLASS
    is_teacher = request.membership.role == SchoolMembership.Role.TEACHER
    messaging_on = thread_messaging_enabled(thread)
    can_post = messaging_on and not (is_class and thread.announcements_only and not is_teacher)

    if request.method == "POST":
        if not messaging_on:
            messages.error(
                request,
                "Messaging is currently turned off — this conversation is read-only "
                "until it's turned back on.",
            )
            return redirect("core:message_thread_detail", pk=thread.pk)
        if not can_post:
            messages.error(
                request, "This thread is announcements only — only the teacher can post."
            )
            return redirect("core:message_thread_detail", pk=thread.pk)
        body = request.POST.get("body", "").strip()
        if not body:
            messages.error(request, "Type a message before sending.")
        else:
            Message.objects.create(thread=thread, sender=request.user, body=body)
            thread.last_message_at = timezone.now()
            thread.save(update_fields=["last_message_at"])

            # Notify whoever else is in the thread — never the sender
            # themselves. A class thread can have many participants; a
            # one-to-one thread has exactly one "other party" (whichever
            # of guardian/teacher isn't request.user).
            if is_class:
                recipients = thread.participants.exclude(id=request.user.id)
            else:
                other_id = thread.teacher_id if request.user.id == thread.guardian_id else thread.guardian_id
                recipients = User.objects.filter(id=other_id)
            sender_name = request.user.get_full_name() or request.user.username
            notify_users(
                recipients,
                title=f"New message from {sender_name}",
                body=body[:150],
                url=reverse("core:message_thread_detail", args=[thread.pk]),
            )
        return redirect("core:message_thread_detail", pk=thread.pk)

    thread_messages = list(thread.messages.select_related("sender").order_by("sent_at"))
    other_party = None
    participants = []

    if is_class:
        MessageThreadRead.objects.update_or_create(
            thread=thread, user=request.user, defaults={"last_read_at": timezone.now()}
        )
        participants = list(thread.participants.order_by("last_name", "first_name"))
    else:
        unread_ids = [
            m.pk for m in thread_messages if m.sender_id != request.user.id and m.read_at is None
        ]
        if unread_ids:
            now = timezone.now()
            Message.objects.filter(pk__in=unread_ids).update(read_at=now)
            for m in thread_messages:
                if m.pk in unread_ids:
                    m.read_at = now
        other_party = thread.teacher if thread.guardian_id == request.user.id else thread.guardian

    return render(
        request,
        "core/message_thread_detail.html",
        {
            "thread": thread,
            "thread_messages": thread_messages,
            "other_party": other_party,
            "participants": participants,
            "is_class": is_class,
            "can_post": can_post,
            "messaging_on": messaging_on,
            "can_moderate": is_class and is_teacher,
        },
    )


@login_required
@role_required(SchoolMembership.Role.TEACHER)
@require_POST
def class_message_remove(request, pk, message_pk):
    """
    Teacher-only "remove message" moderation action for a class thread
    (proposal: "Class Group Messaging" — "the teacher stays the
    backstop, consistent with how a real classroom group works").
    Sets Message.removed_at/removed_by rather than deleting the row —
    the template swaps a removed message's body for a visible
    placeholder — so the rest of the thread doesn't lose context
    mid-conversation. Idempotent: removing an already-removed message a
    second time is a no-op, not an error.

    Re-derives thread membership fresh via _get_message_thread_or_404
    rather than trusting that a pk in the URL is one this teacher was
    actually shown a "Remove" button for, and separately checks
    thread_type — a one-to-one thread has no moderation feature, so this
    can't be used against one even if a class-thread message_pk-shaped
    URL were guessed against it.

    Not gated on thread_messaging_enabled — moderating (cleaning up)
    history that's currently read-only is still useful, and doesn't let
    anyone post, so there's no reason to also block it while the
    on/off switch is off.
    """
    if request.school is None:
        raise Http404
    thread = _get_message_thread_or_404(request, pk)
    if thread.thread_type != MessageThread.ThreadType.CLASS:
        raise Http404
    message = get_object_or_404(Message.objects.filter(thread=thread), pk=message_pk)
    if not message.removed_at:
        message.removed_at = timezone.now()
        message.removed_by = request.user
        message.save(update_fields=["removed_at", "removed_by"])
    messages.success(request, "Message removed.")
    return redirect("core:message_thread_detail", pk=thread.pk)


@login_required
@role_required(SchoolMembership.Role.TEACHER)
@require_POST
def class_thread_toggle_announcements_only(request, pk):
    """
    Teacher-only per-class toggle between the default (guardians can
    post) and "announcements only" (teacher posts, guardians read) —
    the escape hatch the proposal calls out for "a class's dynamics"
    that don't suit open two-way chat. A plain toggle, not a form:
    there's exactly one bit to flip and no other field involved.
    """
    if request.school is None:
        raise Http404
    thread = _get_message_thread_or_404(request, pk)
    if thread.thread_type != MessageThread.ThreadType.CLASS:
        raise Http404
    thread.announcements_only = not thread.announcements_only
    thread.save(update_fields=["announcements_only"])
    if thread.announcements_only:
        messages.success(request, "Switched to announcements only — only you can post now.")
    else:
        messages.success(request, "Guardians can post in this thread again.")
    return redirect("core:message_thread_detail", pk=thread.pk)


@login_required
def wiki(request):
    """
    In-app user manual — static documentation, not scoped to a school or
    role. Deliberately just @login_required (no role_required): unlike
    every other real view in this app, there's nothing here that differs
    per school or needs hiding from any particular role — an admin
    reading how Teachers work and a teacher reading how Guardians work
    are both fine. It exists so "how do I do X" has an answer inside the
    product itself, not only in the handover plan doc a user account
    holder wouldn't have access to.
    """
    return render(request, "core/wiki.html")


# ---------------------------------------------------------------------
# App & Notifications — the last Phase 1 build-sequence item (proposal:
# "PWA install + web push notifications"). Installability itself
# (manifest + service worker, root_static/sw.js) doesn't need a view —
# a browser offers to install any page that meets the criteria on its
# own — so what lives here is just the piece that does need a server:
# saving/removing a browser's Web Push subscription (core.models.
# PushSubscription), plus one page explaining both halves to a user.
#
# Deliberately @login_required only, no role_required, same reasoning
# as core.views.wiki: an admin, teacher, and guardian all read/write
# their own notification preference identically, and none of it differs
# by role or school.
# ---------------------------------------------------------------------

@login_required
def app_notifications(request):
    """
    One page covering both halves of this feature: installing Notipa as
    an app (the manifest/service-worker already make this possible;
    this page mostly explains *how*, since Android/desktop and iOS get
    there differently), and enabling push notifications on this device.
    Whether *this particular* subscription is currently active is
    decided client-side (core/static/core/js/push.js checks
    PushManager.getSubscription() on load) — the server only ever sees
    "a subscription with this endpoint exists or it doesn't," not which
    device a browser tab is running on, so the enabled/disabled toggle
    itself is JS-driven rather than computed here.
    """
    return render(
        request,
        "core/app_notifications.html",
        {"push_configured": push_configured(), "subscription_count": request.user.push_subscriptions.count()},
    )


@login_required
@require_POST
def push_subscribe(request):
    """
    Saves (or updates) the Web Push subscription the browser just
    created via PushManager.subscribe() — core/static/core/js/push.js
    posts the subscription's own JSON shape here as soon as the user
    grants permission. `endpoint` is the upsert key (see PushSubscription's
    docstring for why it's safely unique across users): a browser that
    already has a row here re-subscribing (e.g. the browser rotated the
    subscription under the hood) updates its keys in place rather than
    creating a duplicate a user would otherwise start getting double
    notifications from.
    """
    try:
        payload = json.loads(request.body)
        endpoint = payload["endpoint"]
        p256dh = payload["keys"]["p256dh"]
        auth_key = payload["keys"]["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "Malformed subscription payload."}, status=400)

    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": request.user,
            "p256dh": p256dh,
            "auth_key": auth_key,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:255],
        },
    )
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def push_unsubscribe(request):
    """
    Removes a Web Push subscription — either the user explicitly turning
    notifications off for this device, or the service worker cleaning
    up after PushManager.subscription.unsubscribe() succeeds client-side.
    Scoped to request.user: a user can only ever remove their own
    subscription, never one belonging to someone else, even if they
    somehow got hold of another endpoint string.
    """
    try:
        payload = json.loads(request.body)
        endpoint = payload["endpoint"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": "Malformed request."}, status=400)

    PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def switch_school(request):
    """
    Changes request.session['active_school_id'] to a school the
    logged-in user actually has an active membership at, then redirects
    back where they came from. Only ever trusts school IDs that appear
    in request.memberships (set by ActiveSchoolMiddleware from the
    user's own SchoolMembership rows) — never the raw POSTed value —
    so this can't be used to view another school's data by guessing IDs.
    """
    school_id = request.POST.get("school_id", "")
    valid_ids = {str(m.school_id) for m in request.memberships}
    if school_id in valid_ids:
        request.session["active_school_id"] = school_id

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
    if next_url:
        return HttpResponseRedirect(next_url)
    return redirect("core:dashboard")


# ---------------------------------------------------------------------
# School setup — lets a superuser create a School (the tenant itself)
# from inside the app instead of via /admin/. This is the fix for a
# brand new superuser otherwise being stuck: createsuperuser gives you
# a login, but no School and no SchoolMembership exist yet, and until
# this view existed the only way to create the first one was the
# Django admin — exactly the "users creating records directly in the
# database" experience this avoids.
# ---------------------------------------------------------------------

@login_required
@superuser_required
def school_setup(request):
    if request.method == "POST":
        form = SchoolForm(request.POST)
        if form.is_valid():
            school = form.save()
            SchoolMembership.objects.create(
                user=request.user, school=school, role=SchoolMembership.Role.ADMIN
            )
            request.session["active_school_id"] = str(school.id)
            messages.success(
                request, f"“{school.name}” created — you're set as its admin."
            )
            return redirect("core:dashboard")
    else:
        form = SchoolForm()

    # Only a superuser ever reaches this view (superuser_required, above),
    # so listing every school in the system here — not just ones the
    # current user belongs to — isn't a school-scoping leak: this is
    # exactly the platform-operator context (creating a new tenant) where
    # seeing the full tenant list is the point, e.g. to confirm a school
    # doesn't already exist before adding a near-duplicate. Annotated
    # counts use active-only students/classes to match what the rest of
    # the app treats as the "real" count (students_list/classes_list both
    # default to is_active=True).
    schools = School.objects.annotate(
        active_student_count=Count(
            "students", filter=Q(students__is_active=True), distinct=True
        ),
        active_class_count=Count(
            "classes", filter=Q(classes__is_active=True), distinct=True
        ),
    ).order_by("name")

    return render(request, "core/school_setup.html", {"form": form, "schools": schools})


# ---------------------------------------------------------------------
# Settings — the last of the placeholder sidebar sections. Unlike
# school_setup (superuser-only, creates a *new* tenant), this edits the
# *currently active* school's own profile, and is reachable by a
# regular school admin — matching the Settings sidebar link's existing
# admin-only visibility (templates/base.html). There's no id in the
# URL at all: the view always operates on request.school, the same
# "never trust an id from outside the session/middleware" posture
# switch_school already uses, just taken one step further since there's
# nothing here to even pass in.
# ---------------------------------------------------------------------

@login_required
@role_required(SchoolMembership.Role.ADMIN)
def school_settings(request):
    if request.school is None:
        messages.error(request, "You need to be linked to a school to view settings.")
        return redirect("core:dashboard")

    if request.method == "POST":
        form = SchoolSettingsForm(request.POST, instance=request.school)
        if form.is_valid():
            form.save()
            messages.success(request, f"“{request.school.name}” settings updated.")
            return redirect("core:settings")
    else:
        form = SchoolSettingsForm(instance=request.school)

    return render(
        request,
        "core/settings.html",
        {"form": form, "school": request.school, "app_version": django_settings.APP_VERSION},
    )


# ---------------------------------------------------------------------
# Teachers — the in-app staff invite flow flagged in the handover plan
# (Section 10 / Section 9 item 2) as the remaining onboarding gap:
# classes reference a homeroom_teacher, so a school needs a way to add
# teachers before classes are fully usable. Restricted to admin only
# (not teacher) — adding staff, unlike adding students/classes, is an
# admin-level action; a teacher shouldn't be able to grant themselves
# or anyone else admin access to the school.
#
# "Soft delete" for a teacher is just the is_active flag SchoolMembership
# already had ("revoke access without deleting history") — teacher_revoke
# and teacher_restore toggle it, rather than introducing a second notion
# of deletion. is_active=False rows are still shown in teachers_list
# (see the Status column) so a revoked membership isn't a dead end.
# ---------------------------------------------------------------------

@login_required
@role_required(SchoolMembership.Role.ADMIN)
def teachers_list(request):
    memberships = scope_to_school(
        SchoolMembership.objects.filter(
            role__in=[SchoolMembership.Role.TEACHER, SchoolMembership.Role.ADMIN]
        ).select_related("user"),
        request,
    ).order_by("role", "user__last_name", "user__first_name")
    return render(request, "core/teachers_list.html", {"memberships": memberships})


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def teacher_new(request):
    if request.school is None:
        messages.error(request, "You need to be linked to a school before you can add a teacher.")
        return redirect("core:dashboard")

    if request.method == "POST":
        form = TeacherForm(request.POST, school=request.school)
        if form.is_valid():
            membership = form.save()
            messages.success(
                request,
                f"{membership.user.get_full_name() or membership.user.username} added as "
                f"{membership.get_role_display()}.",
            )
            return redirect("core:teachers")
    else:
        form = TeacherForm(school=request.school)

    return render(request, "core/teacher_form.html", {"form": form})


def _get_membership_or_404(request, pk):
    """Looks up a SchoolMembership by pk, scoped to request.school — a
    404 (not a 403) for a pk that belongs to another school, so this
    can't be used to even confirm another school's staff exist."""
    return get_object_or_404(
        scope_to_school(
            SchoolMembership.objects.filter(
                role__in=[SchoolMembership.Role.TEACHER, SchoolMembership.Role.ADMIN]
            ).select_related("user"),
            request,
        ),
        pk=pk,
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN)
def teacher_edit(request, pk):
    membership = _get_membership_or_404(request, pk)

    if request.method == "POST":
        form = TeacherEditForm(request.POST, membership=membership)
        if form.is_valid():
            form.save()
            messages.success(request, f"{membership.user} updated.")
            return redirect("core:teachers")
    else:
        form = TeacherEditForm(membership=membership)

    return render(
        request, "core/teacher_edit_form.html", {"form": form, "membership": membership}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN)
@require_POST
def teacher_revoke(request, pk):
    membership = _get_membership_or_404(request, pk)
    membership.is_active = False
    membership.save(update_fields=["is_active"])
    messages.success(request, f"{membership.user}'s access has been revoked.")
    return redirect("core:teachers")


@login_required
@role_required(SchoolMembership.Role.ADMIN)
@require_POST
def teacher_restore(request, pk):
    membership = _get_membership_or_404(request, pk)
    membership.is_active = True
    membership.save(update_fields=["is_active"])
    messages.success(request, f"{membership.user}'s access has been restored.")
    return redirect("core:teachers")


# ---------------------------------------------------------------------
# Classes — Phase 1 build sequence item 3 (admin/teacher onboarding).
# Restricted to admin/teacher: guardians have no reason to browse the
# full class roster, and role_required + scope_to_school together are
# what actually enforce that, not just hiding the sidebar link.
#
# Archiving a class (is_active=False) is the soft-delete: it drops out
# of the default list and out of the homeroom-teacher-eligible /
# school-class-eligible dropdowns (SchoolClassForm/StudentForm already
# filter on is_active=True), but existing students keep pointing at it
# and its history isn't touched — same "revoke, don't erase" reasoning
# as teacher_revoke.
# ---------------------------------------------------------------------

@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def classes_list(request):
    show_archived = request.GET.get("show") == "archived"
    classes = scope_to_school(
        SchoolClass.objects.select_related("homeroom_teacher__user"), request
    )
    if not show_archived:
        classes = classes.filter(is_active=True)
    classes = classes.order_by("academic_year", "name")
    return render(
        request, "core/classes_list.html", {"classes": classes, "show_archived": show_archived}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def class_new(request):
    if request.school is None:
        messages.error(request, "You need to be linked to a school before you can add a class.")
        return redirect("core:dashboard")

    if request.method == "POST":
        form = SchoolClassForm(request.POST, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(request, f"Class “{form.instance.name}” created.")
            return redirect("core:classes")
    else:
        form = SchoolClassForm(school=request.school)

    return render(request, "core/class_form.html", {"form": form})


def _get_class_or_404(request, pk):
    return get_object_or_404(scope_to_school(SchoolClass.objects.all(), request), pk=pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def class_detail(request, pk):
    """
    Single-class view: homeroom teacher, any co-teachers, and its
    roster — the thing classes_list couldn't show without cramming a
    student list into every row. Reuses the same school-scoping
    (_get_class_or_404) and the same active/archived toggle pattern as
    students_list, so a class with archived students isn't confusing
    about where they went.
    """
    school_class = get_object_or_404(
        scope_to_school(
            SchoolClass.objects.select_related("homeroom_teacher__user").prefetch_related(
                "additional_teachers__user"
            ),
            request,
        ),
        pk=pk,
    )

    show_archived = request.GET.get("show") == "archived"
    students = school_class.students.all()
    if not show_archived:
        students = students.filter(is_active=True)
    students = students.order_by("last_name", "first_name")

    # This class's group thread, if it exists and the requesting user is
    # a connected teacher on it (homeroom or co-teacher) — an admin
    # viewing the same page doesn't get a link, since class messaging is
    # scoped to teacher + guardians only (proposal: "Class Group
    # Messaging"), the same way admins don't appear in
    # _teacher_ids_for_class at all. Gated on effective-enabled (school
    # switch AND this class's own switch), not just the school switch —
    # this is a "start/open a conversation" entry point, which the
    # per-school/per-class switch proposal says to hide when either is
    # off; an already-existing thread stays reachable read-only via the
    # Messages inbox regardless.
    class_thread = None
    if (
        request.membership.role == SchoolMembership.Role.TEACHER
        and request.user.id in _teacher_ids_for_class(school_class)
        and class_messaging_effectively_enabled(school_class)
    ):
        class_thread = MessageThread.objects.filter(
            school=request.school,
            thread_type=MessageThread.ThreadType.CLASS,
            school_class=school_class,
        ).first()

    # "Not yet taken today" indicator (roadmap: Attendance Tracking) —
    # a teacher shouldn't have to rely on memory to know whether
    # they've already run today's roster for this class.
    today = timezone.localdate()
    attendance_taken_today = AttendanceRecord.objects.filter(
        school_class=school_class, date=today
    ).exists()

    return render(
        request,
        "core/class_detail.html",
        {
            "school_class": school_class,
            "students": students,
            "show_archived": show_archived,
            "class_thread": class_thread,
            "attendance_taken_today": attendance_taken_today,
        },
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def class_edit(request, pk):
    school_class = _get_class_or_404(request, pk)

    if request.method == "POST":
        form = SchoolClassForm(request.POST, instance=school_class, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(request, f"Class “{form.instance.name}” updated.")
            return redirect("core:classes")
    else:
        form = SchoolClassForm(instance=school_class, school=request.school)

    return render(
        request, "core/class_form.html", {"form": form, "school_class": school_class}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def class_archive(request, pk):
    school_class = _get_class_or_404(request, pk)
    school_class.is_active = False
    school_class.save(update_fields=["is_active"])
    messages.success(request, f"Class “{school_class.name}” archived.")
    return redirect("core:classes")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def class_restore(request, pk):
    school_class = _get_class_or_404(request, pk)
    school_class.is_active = True
    school_class.save(update_fields=["is_active"])
    messages.success(request, f"Class “{school_class.name}” restored.")
    return redirect("core:classes")


# ---------------------------------------------------------------------
# Students — same restriction as Classes, same reasoning. Archiving
# (is_active=False) is the soft-delete here too: it's what StudentForm
# would otherwise call "left the school" without losing their guardian
# links, records, fee notices, or permission-slip history.
# ---------------------------------------------------------------------

@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def students_list(request):
    show_archived = request.GET.get("show") == "archived"
    students = scope_to_school(
        Student.objects.select_related("school_class"), request
    )
    if not show_archived:
        students = students.filter(is_active=True)
    students = students.order_by("last_name", "first_name")
    return render(
        request, "core/students_list.html", {"students": students, "show_archived": show_archived}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def student_new(request):
    if request.school is None:
        messages.error(request, "You need to be linked to a school before you can add a student.")
        return redirect("core:dashboard")

    if request.method == "POST":
        form = StudentForm(request.POST, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(
                request, f"{form.instance.first_name} {form.instance.last_name} added."
            )
            return redirect("core:students")
    else:
        form = StudentForm(school=request.school)

    return render(request, "core/student_form.html", {"form": form})


def _get_student_or_404(request, pk):
    return get_object_or_404(scope_to_school(Student.objects.all(), request), pk=pk)


def _guardian_links_with_membership_pk(request, guardian_links):
    """
    Attaches `guardian_membership_pk` to each GuardianLink in the given
    (already-evaluated) list, so student_detail.html can link a
    guardian's name straight to their guardian_detail page (which is
    keyed by SchoolMembership pk, not User pk — same as everywhere else
    a person is looked up in this app). One query for the whole list
    rather than one per row.
    """
    guardian_links = list(guardian_links)
    membership_pk_by_user_id = dict(
        SchoolMembership.objects.filter(
            user_id__in=[link.guardian_id for link in guardian_links],
            school=request.school,
            role=SchoolMembership.Role.GUARDIAN,
        ).values_list("user_id", "pk")
    )
    for link in guardian_links:
        link.guardian_membership_pk = membership_pk_by_user_id.get(link.guardian_id)
    return guardian_links


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def student_detail(request, pk):
    """
    Single-student view: the student's own details, plus every guardian
    linked to them (relationship, primary-contact flag) and a form to
    link another one. This is what makes Guardian setup (core.forms.
    GuardianForm) actually useful — a guardian account existing doesn't
    mean anything to a student until a GuardianLink connects the two.
    """
    student = _get_student_or_404(request, pk)
    guardian_links = _guardian_links_with_membership_pk(
        request,
        student.guardian_links.select_related("guardian").order_by(
            "-is_primary_contact", "guardian__last_name", "guardian__first_name"
        ),
    )
    form = GuardianLinkForm(student=student)
    # Only offer a "Message" link to a teacher who's actually connected
    # to this student (homeroom or co-teacher of their class) — a
    # teacher browsing an unrelated student's page shouldn't see a
    # button that 404s the moment they click it; message_thread_start
    # re-checks this same connection server-side regardless.
    can_message = bool(
        request.membership
        and request.membership.role == SchoolMembership.Role.TEACHER
        and request.user.id in _connected_teacher_ids_for_student(student)
        and class_messaging_effectively_enabled(student.school_class)
    )
    return render(
        request,
        "core/student_detail.html",
        {
            "student": student,
            "guardian_links": guardian_links,
            "form": form,
            "can_message": can_message,
        },
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def guardian_link_add(request, pk):
    student = _get_student_or_404(request, pk)
    form = GuardianLinkForm(request.POST, student=student)
    if form.is_valid():
        link = form.save()
        messages.success(
            request,
            f"{link.guardian.get_full_name() or link.guardian.username} linked to "
            f"{student.first_name} {student.last_name}.",
        )
        return redirect("core:student_detail", pk=student.pk)

    guardian_links = _guardian_links_with_membership_pk(
        request,
        student.guardian_links.select_related("guardian").order_by(
            "-is_primary_contact", "guardian__last_name", "guardian__first_name"
        ),
    )
    return render(
        request,
        "core/student_detail.html",
        {"student": student, "guardian_links": guardian_links, "form": form},
    )


def _get_guardian_link_or_404(request, student_pk, link_pk):
    """Scoped two ways: the student must belong to request.school
    (_get_student_or_404), and the link must belong to that student —
    so a link id from another school's roster can't be targeted even
    if guessed."""
    student = _get_student_or_404(request, student_pk)
    return get_object_or_404(GuardianLink.objects.filter(student=student), pk=link_pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def guardian_link_remove(request, pk, link_pk):
    link = _get_guardian_link_or_404(request, pk, link_pk)
    guardian_name = link.guardian.get_full_name() or link.guardian.username
    student = link.student
    link.delete()
    messages.success(request, f"{guardian_name} removed from {student.first_name} {student.last_name}.")
    return redirect("core:student_detail", pk=student.pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def student_edit(request, pk):
    student = _get_student_or_404(request, pk)

    if request.method == "POST":
        form = StudentForm(request.POST, instance=student, school=request.school)
        if form.is_valid():
            form.save()
            messages.success(
                request, f"{form.instance.first_name} {form.instance.last_name} updated."
            )
            return redirect("core:students")
    else:
        form = StudentForm(instance=student, school=request.school)

    return render(request, "core/student_form.html", {"form": form, "student": student})


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def student_archive(request, pk):
    student = _get_student_or_404(request, pk)
    student.is_active = False
    student.save(update_fields=["is_active"])
    messages.success(request, f"{student.first_name} {student.last_name} archived.")
    return redirect("core:students")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def student_restore(request, pk):
    student = _get_student_or_404(request, pk)
    student.is_active = True
    student.save(update_fields=["is_active"])
    messages.success(request, f"{student.first_name} {student.last_name} restored.")
    return redirect("core:students")


# ---------------------------------------------------------------------
# Guardians — the prerequisite for linking a guardian to a Student
# (core.models.GuardianLink, the next piece of work after this one):
# there was no in-app way to create a guardian account at all, only
# /admin/. Restricted to admin/teacher, same as Classes/Students —
# unlike adding a teacher/admin (staff, a privilege-escalation concern),
# adding a guardian is routine roster work done when a family enrolls,
# so it doesn't need the tighter admin-only restriction teacher_new has.
#
# Soft delete mirrors teacher_revoke/teacher_restore: guardian_revoke/
# guardian_restore toggle the same SchoolMembership.is_active flag,
# rather than a second notion of deletion.
# ---------------------------------------------------------------------

@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def guardians_list(request):
    memberships = scope_to_school(
        SchoolMembership.objects.filter(role=SchoolMembership.Role.GUARDIAN).select_related(
            "user"
        ),
        request,
    ).order_by("user__last_name", "user__first_name")
    return render(request, "core/guardians_list.html", {"memberships": memberships})


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def guardian_new(request):
    if request.school is None:
        messages.error(request, "You need to be linked to a school before you can add a guardian.")
        return redirect("core:dashboard")

    if request.method == "POST":
        form = GuardianForm(request.POST, school=request.school)
        if form.is_valid():
            membership = form.save()
            messages.success(
                request, f"{membership.user.get_full_name() or membership.user.username} added as a guardian."
            )
            return redirect("core:guardians")
    else:
        form = GuardianForm(school=request.school)

    return render(request, "core/guardian_form.html", {"form": form})


def _get_guardian_membership_or_404(request, pk):
    """Looks up a GUARDIAN SchoolMembership by pk, scoped to
    request.school — a 404 (not a 403) for a pk belonging to another
    school, same reasoning as _get_membership_or_404 for teachers."""
    return get_object_or_404(
        scope_to_school(
            SchoolMembership.objects.filter(
                role=SchoolMembership.Role.GUARDIAN
            ).select_related("user"),
            request,
        ),
        pk=pk,
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def guardian_detail(request, pk):
    """
    Single-guardian view: the mirror of student_detail. Lets a guardian
    be linked to a student from the guardian's own page, not just from
    the student's — needed because a guardian is often enrolling more
    than one child (siblings) at once, and adding each one from their
    own student page is the wrong direction to start from in that case.
    Same underlying GuardianLink record either way.
    """
    membership = _get_guardian_membership_or_404(request, pk)
    guardian_links = (
        membership.user.guardian_links.filter(student__school=request.school)
        .select_related("student")
        .order_by("student__last_name", "student__first_name")
    )
    form = StudentLinkForm(guardian=membership.user, school=request.school)
    return render(
        request,
        "core/guardian_detail.html",
        {"membership": membership, "guardian_links": guardian_links, "form": form},
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def student_link_add(request, pk):
    membership = _get_guardian_membership_or_404(request, pk)
    form = StudentLinkForm(request.POST, guardian=membership.user, school=request.school)
    if form.is_valid():
        link = form.save()
        messages.success(
            request,
            f"{link.student.first_name} {link.student.last_name} linked to "
            f"{membership.user.get_full_name() or membership.user.username}.",
        )
        return redirect("core:guardian_detail", pk=membership.pk)

    guardian_links = (
        membership.user.guardian_links.filter(student__school=request.school)
        .select_related("student")
        .order_by("student__last_name", "student__first_name")
    )
    return render(
        request,
        "core/guardian_detail.html",
        {"membership": membership, "guardian_links": guardian_links, "form": form},
    )


def _get_guardian_student_link_or_404(request, membership_pk, link_pk):
    """Scoped two ways, mirroring _get_guardian_link_or_404: the
    membership must belong to request.school, and the link must belong
    to that membership's user — so a link id from another guardian (or
    another school) can't be targeted even if guessed."""
    membership = _get_guardian_membership_or_404(request, membership_pk)
    return get_object_or_404(
        GuardianLink.objects.filter(guardian=membership.user, student__school=request.school),
        pk=link_pk,
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def student_link_remove(request, pk, link_pk):
    link = _get_guardian_student_link_or_404(request, pk, link_pk)
    student_name = f"{link.student.first_name} {link.student.last_name}"
    link.delete()
    messages.success(request, f"{student_name} removed from this guardian.")
    return redirect("core:guardian_detail", pk=pk)


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
def guardian_edit(request, pk):
    membership = _get_guardian_membership_or_404(request, pk)

    if request.method == "POST":
        form = GuardianEditForm(request.POST, membership=membership)
        if form.is_valid():
            form.save()
            messages.success(request, f"{membership.user} updated.")
            return redirect("core:guardians")
    else:
        form = GuardianEditForm(membership=membership)

    return render(
        request, "core/guardian_edit_form.html", {"form": form, "membership": membership}
    )


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def guardian_revoke(request, pk):
    membership = _get_guardian_membership_or_404(request, pk)
    membership.is_active = False
    membership.save(update_fields=["is_active"])
    messages.success(request, f"{membership.user}'s access has been revoked.")
    return redirect("core:guardians")


@login_required
@role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
@require_POST
def guardian_restore(request, pk):
    membership = _get_guardian_membership_or_404(request, pk)
    membership.is_active = True
    membership.save(update_fields=["is_active"])
    messages.success(request, f"{membership.user}'s access has been restored.")
    return redirect("core:guardians")


def offline(request):
    """
    Fallback page the PWA service worker (root_static/sw.js) serves for
    page navigations when the network is unreachable and nothing cached
    matches the requested URL. Deliberately not @login_required — a
    logged-out or session-expired browser can still be offline, and this
    page has no data of its own to protect.
    """
    return render(request, "core/offline.html")
