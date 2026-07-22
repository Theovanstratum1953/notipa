from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    AnnouncementForm,
    FeeNoticeForm,
    GuardianEditForm,
    GuardianForm,
    GuardianLinkForm,
    HomeworkForm,
    PermissionSlipForm,
    SchoolClassForm,
    SchoolForm,
    SchoolSettingsForm,
    StudentForm,
    StudentLinkForm,
    TeacherEditForm,
    TeacherForm,
)
from .models import (
    Announcement,
    AnnouncementRead,
    FeeNotice,
    GuardianLink,
    Homework,
    PermissionSlip,
    PermissionSlipResponse,
    School,
    SchoolClass,
    SchoolMembership,
    Student,
)
from .permissions import role_required, scope_to_school, superuser_required


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
    class_ids = _relevant_class_ids_for_guardian(request)
    homework_items = (
        Homework.objects.filter(school_class_id__in=class_ids)
        .select_related("school_class")
        .order_by("-created_at")
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

    return render(request, "core/settings.html", {"form": form, "school": request.school})


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

    return render(
        request,
        "core/class_detail.html",
        {"school_class": school_class, "students": students, "show_archived": show_archived},
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
    return render(
        request,
        "core/student_detail.html",
        {"student": student, "guardian_links": guardian_links, "form": form},
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
