"""
Core data model for Notipa.

Implements the multi-tenant School/class/student/guardian schema described
in the proposal (Section 4.1) and the handover plan (Section 9, item 2 —
"the part not to rush"): every tenant-scoped model carries a `school`
foreign key (row-level multi-tenancy, per proposal Section 5), and roles
(admin / teacher / guardian) are modelled per-school via SchoolMembership
rather than as global Django permissions — the same person can hold
different roles at different schools, or occasionally more than one role
at the same school (e.g. a staff member whose own child attends).

Every model uses a UUID primary key rather than the default sequential
integer: this is data about children and their guardians, so IDs that
can't be enumerated by incrementing a URL are worth the (small) cost —
see the handover notes. It also avoids ID collisions if data ever needs
to move between separate self-hosted instances (proposal Section 8).

Queryset-level scoping (a teacher at School A cannot query School B's
data) is enforced in views/managers built on top of these models, not
here — this file is the schema those permission checks rely on.
"""
import os
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


MAX_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Extensions a guardian's homework submission can be — deliberately
# narrower than teacher-side Homework.attachment (which stays
# unrestricted, since a teacher might post a worksheet in any format).
# Guardians are a less-trusted upload path and the pilot only ever
# needs "a photo of the completed page" or "a scanned PDF" (proposal's
# "conservative defaults" note, since schools may be self-hosting on
# modest hardware/bandwidth).
SUBMISSION_ALLOWED_EXTENSIONS = [".jpg", ".jpeg", ".png", ".heic", ".heif", ".pdf"]


def validate_attachment_size(value):
    """Shared size cap for homework attachments and submissions — one
    file pipeline, one limit, rather than a second set of rules for the
    guardian-facing upload."""
    if value.size > MAX_ATTACHMENT_SIZE_BYTES:
        raise ValidationError(
            f"File is too large ({value.size / (1024 * 1024):.1f} MB). "
            f"The limit is {MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)} MB."
        )


def validate_submission_extension(value):
    ext = os.path.splitext(value.name)[1].lower()
    if ext not in SUBMISSION_ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file type “{ext or 'unknown'}”. Upload an image or a PDF."
        )


class UUIDModel(models.Model):
    """Abstract base giving every concrete model a UUID primary key
    instead of Django's default auto-incrementing integer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class User(AbstractUser, UUIDModel):
    """
    Custom user model shared by admins, teachers, and guardians.

    Email is not required, since guardians are commonly invited via
    SMS/link with no email address at all (proposal Section 4.2:
    "invite parents via SMS/link — no app-store account required").
    phone_number is the identifier that matters for that flow. Which
    role(s) a user holds is determined per-school via SchoolMembership,
    not on this record.
    """

    phone_number = models.CharField(
        max_length=32,
        blank=True,
        help_text=(
            "E.164 format preferred, e.g. +639171234567. Primary "
            "identifier for guardians invited via SMS/link."
        ),
    )
    preferred_language = models.CharField(
        max_length=10,
        blank=True,
        help_text=(
            "ISO language code (e.g. 'en', 'fil'). Empty means fall back "
            "to the school's default_language."
        ),
    )

    def __str__(self):
        return self.get_full_name() or self.username or self.phone_number or f"User {self.pk}"


class School(UUIDModel):
    """The tenant. Every other model in this app is scoped to one School,
    directly or (for records that hang off a class/student) transitively."""

    class Tier(models.TextChoices):
        FREE = "free", "Free (Track 1 — public school)"
        PAID = "paid", "Paid (Track 2 — private school licence)"

    name = models.CharField(max_length=255)
    country = models.CharField(
        max_length=2,
        help_text="ISO 3166-1 alpha-2 country code, e.g. 'PH'.",
    )
    default_language = models.CharField(
        max_length=10,
        default="en",
        help_text="ISO language code used when a user has no preferred_language set.",
    )
    timezone = models.CharField(max_length=64, default="Asia/Manila")
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.FREE)
    academic_year_start_month = models.PositiveSmallIntegerField(
        default=6,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
        help_text="Month (1-12) the school's academic year starts, e.g. 6 for June.",
    )
    messaging_enabled = models.BooleanField(
        default=False,
        help_text=(
            "The master switch for direct guardian ↔ teacher messaging and "
            "class group messaging at this school (proposal: 'Per-School "
            "Messaging On/Off Switch'). Off by default: schools that aren't "
            "ready to support real-time parent messaging (staffing, "
            "moderation, expectations around response time) shouldn't have "
            "it forced on them. Turning it on is a real commitment — someone "
            "has to be expected to actually read and respond. A prerequisite "
            "for MessageThread/Message below, not just a UI hint — "
            "thread-creation, posting, and (via SchoolClass.messaging_enabled) "
            "the per-class override are all re-checked server-side, never "
            "trusted from a hidden button alone. Turning this off doesn't "
            "delete any existing thread or message — see core.messaging."
            "thread_messaging_enabled — it only stops new ones."
        ),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SchoolMembership(UUIDModel):
    """
    Links a User to a School with a role. This is the enforcement point for
    school-scoped permissions (proposal Section 5): filtering querysets to
    memberships for the requesting user's school(s) is how a teacher at
    School A is structurally prevented from reaching School B's data,
    rather than that boundary being UI-hidden only.
    """

    class Role(models.TextChoices):
        ADMIN = "admin", "School admin"
        TEACHER = "teacher", "Teacher"
        GUARDIAN = "guardian", "Guardian"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=Role.choices)
    is_active = models.BooleanField(
        default=True,
        help_text=(
            "Set False to revoke access without deleting history "
            "(e.g. a teacher who has left the school)."
        ),
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "school", "role"], name="unique_user_school_role"
            ),
        ]
        indexes = [
            models.Index(fields=["school", "role"]),
        ]

    def __str__(self):
        return f"{self.user} — {self.get_role_display()} at {self.school}"


class SchoolClass(UUIDModel):
    """
    A class/section within a school. Named SchoolClass (not Class) to
    avoid shadowing the Python keyword.
    """

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="classes")
    name = models.CharField(max_length=100, help_text="e.g. 'Grade 4 - Sampaguita'")
    academic_year = models.CharField(max_length=9, help_text="e.g. '2026-2027'.")
    homeroom_teacher = models.ForeignKey(
        SchoolMembership,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="homeroom_classes",
        limit_choices_to={"role": SchoolMembership.Role.TEACHER},
    )
    additional_teachers = models.ManyToManyField(
        SchoolMembership,
        blank=True,
        related_name="co_taught_classes",
        limit_choices_to={"role": SchoolMembership.Role.TEACHER},
        help_text="Co-teachers beyond the homeroom teacher, if any.",
    )
    messaging_enabled = models.BooleanField(
        default=True,
        help_text=(
            "Per-class opt-out for messaging (proposal: 'Per-School Messaging "
            "On/Off Switch' — the teacher-level override), once the school's "
            "own School.messaging_enabled switch is on. Defaults True: "
            "messaging is opt-out at the class level, not opt-in, so a class "
            "gets it automatically the moment the school does, and a teacher "
            "who doesn't want it for their own class turns it off "
            "specifically rather than every class starting off. Meaningless "
            "while the school switch itself is off — 'no partial state', see "
            "core.messaging.class_messaging_effectively_enabled, which is "
            "always what should actually be checked, never this field alone. "
            "Governs both this class's group thread (Class Group Messaging) "
            "and any guardian-teacher one-to-one thread about one of its "
            "students."
        ),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["academic_year", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "name", "academic_year"],
                name="unique_class_per_school_year",
            ),
        ]
        verbose_name_plural = "school classes"

    def __str__(self):
        return f"{self.name} ({self.academic_year}) — {self.school}"


class Student(UUIDModel):
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="students")
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="students",
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField(null=True, blank=True)
    student_id = models.CharField(
        max_length=50,
        blank=True,
        help_text="The school's own student/LRN identifier, if any.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    guardians = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="GuardianLink",
        related_name="students",
    )

    class Meta:
        ordering = ["last_name", "first_name"]
        indexes = [
            models.Index(fields=["school", "last_name", "first_name"]),
        ]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class GuardianLink(UUIDModel):
    """
    Through model connecting a guardian (User) to a Student. Supports
    multi-guardian households (proposal Section 4.1/4.2): mother, father,
    grandparent, or other relative/helper, each with their own account —
    "common outside nuclear-family Western defaults".
    """

    class Relationship(models.TextChoices):
        MOTHER = "mother", "Mother"
        FATHER = "father", "Father"
        GRANDPARENT = "grandparent", "Grandparent"
        GUARDIAN = "guardian", "Guardian"
        OTHER = "other", "Other relative/helper"

    guardian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guardian_links"
    )
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="guardian_links")
    relationship = models.CharField(
        max_length=20, choices=Relationship.choices, default=Relationship.GUARDIAN
    )
    is_primary_contact = models.BooleanField(
        default=False,
        help_text=(
            "Primary contact for this student — used to decide who "
            "receives SMS fallback first, once that channel exists."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["guardian", "student"], name="unique_guardian_student"
            ),
        ]

    def __str__(self):
        return f"{self.guardian} — {self.get_relationship_display()} of {self.student}"


class Announcement(UUIDModel):
    """School-wide (school_class is null) or class-scoped announcement,
    with per-guardian read tracking via AnnouncementRead."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="announcements")
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="announcements",
        help_text="Leave blank for a school-wide announcement.",
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="announcements_authored",
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    read_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="AnnouncementRead",
        related_name="announcements_read",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["school", "-created_at"]),
        ]

    def __str__(self):
        return self.title


class AnnouncementRead(UUIDModel):
    """Read-tracking record: one row per guardian who has read an announcement."""

    announcement = models.ForeignKey(
        Announcement, on_delete=models.CASCADE, related_name="reads"
    )
    guardian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="announcement_reads"
    )
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["announcement", "guardian"], name="unique_announcement_read"
            ),
        ]

    def __str__(self):
        return f"{self.guardian} read {self.announcement}"


class Homework(UUIDModel):
    school_class = models.ForeignKey(
        SchoolClass, on_delete=models.CASCADE, related_name="homework_items"
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    attachment = models.FileField(
        upload_to="homework/%Y/%m/",
        blank=True,
        null=True,
        validators=[validate_attachment_size],
    )
    accepts_submissions = models.BooleanField(
        default=False,
        help_text=(
            "If on, guardians can attach a file against this homework and "
            "this item's detail page shows a submitted/late/missing roster "
            "for the class, the same way permission-slip responses do. "
            "Off by default — existing homework is unaffected until a "
            "teacher opts in."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="homework_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "homework items"

    def __str__(self):
        return f"{self.title} — {self.school_class}"


class HomeworkSubmission(UUIDModel):
    """
    A guardian's (or, later, a student's) response to one Homework item
    for one Student — the read side of the homework loop, closing what
    was previously one-directional (teacher posts, nobody can send
    anything back). Deliberately small: a file, a note, when it arrived,
    and a status — no rubric, no grade, no plagiarism check (out of
    scope; Notipa stays a communication tool, not a gradebook).

    One row per (homework, student) — a resubmission replaces this row
    in place (new file, new submitted_at, status recomputed) rather than
    accumulating a history of attempts, mirroring how
    PermissionSlipResponse holds one current answer per student rather
    than a log of every time a guardian changed their mind.

    There is deliberately no "missing" value in Status: a student with
    no submission simply has no HomeworkSubmission row at all, and
    "missing" (vs. "not yet due") is computed by the view layer from
    homework.due_date the same way a permission slip's implicit
    "pending" bucket is derived rather than a real ID in some other
    table. Status here only ever needs to distinguish submitted
    (on/before the due date) from late (after it) — both mean "we have
    a file," so there's no manual bookkeeping: it's set once, from
    due_date vs. submitted_at, whenever a submission is created or
    replaced.
    """

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        LATE = "late", "Late"

    homework = models.ForeignKey(
        Homework, on_delete=models.CASCADE, related_name="submissions"
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="homework_submissions"
    )
    file = models.FileField(
        upload_to="homework_submissions/%Y/%m/",
        validators=[validate_attachment_size, validate_submission_extension],
    )
    note = models.TextField(blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="homework_submissions_made",
        help_text=(
            "Which linked guardian actually submitted/replaced this. Any "
            "guardian linked to the student can submit or replace — same "
            "multi-guardian-household support GuardianLink provides "
            "elsewhere — so this is informational for the teacher roster, "
            "not an access restriction."
        ),
    )
    submitted_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices)

    class Meta:
        ordering = ["-submitted_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["homework", "student"], name="unique_submission_per_student"
            ),
        ]

    def __str__(self):
        return f"{self.student} — {self.homework} ({self.get_status_display()})"


class FeeNotice(UUIDModel):
    """
    Informational fee due-date notice. Track 2 / private-school only —
    proposal Section 4.3 explicitly keeps payment processing out of v1,
    so this model never touches money movement, only status tracking.
    """

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PAID = "paid", "Marked paid"
        WAIVED = "waived", "Waived"

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="fee_notices")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="fee_notices")
    title = models.CharField(max_length=255, help_text="e.g. 'Term 2 tuition'")
    description = models.TextField(blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(
        max_length=3, default="PHP", help_text="ISO 4217 currency code."
    )
    due_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNPAID)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="fee_notices_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_date"]
        indexes = [
            models.Index(fields=["school", "status", "due_date"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.student} ({self.get_status_display()})"


class PermissionSlip(UUIDModel):
    """Event requiring guardian acknowledgement/response, tracked per
    student via PermissionSlipResponse."""

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="permission_slips"
    )
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="permission_slips",
        help_text="Leave blank if this applies school-wide.",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    event_date = models.DateField(null=True, blank=True)
    response_deadline = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="permission_slips_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class PermissionSlipResponse(UUIDModel):
    class Response(models.TextChoices):
        PENDING = "pending", "Pending"
        YES = "yes", "Yes / consent given"
        NO = "no", "No / consent withheld"

    permission_slip = models.ForeignKey(
        PermissionSlip, on_delete=models.CASCADE, related_name="responses"
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="permission_slip_responses"
    )
    guardian = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="permission_slip_responses",
    )
    response = models.CharField(
        max_length=10, choices=Response.choices, default=Response.PENDING
    )
    notes = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["permission_slip", "student"],
                name="unique_slip_response_per_student",
            ),
        ]

    def __str__(self):
        return f"{self.permission_slip} — {self.student} ({self.get_response_display()})"


class StudentRecord(UUIDModel):
    """
    Teacher-authored note or attached report, visible only to that
    student's guardians (proposal Section 4.1: "guardian-scoped") unless
    visible_to_guardians is explicitly turned off for an internal-only note.
    """

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="student_records")
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="records")
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    attachment = models.FileField(
        upload_to="student_records/%Y/%m/", blank=True, null=True
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="student_records_authored",
    )
    visible_to_guardians = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["student", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} — {self.student}"


class SchoolCalendarEvent(UUIDModel):
    """
    A closed day (or range of closed days) for a school — public
    holidays, in-service days, school-declared breaks. Deliberately a
    calendar of *exceptions*, not a full scheduling system: no
    timetables, no period-by-period class schedules, and — first
    version — no recurring-event logic ("every Saturday"), since public
    holidays don't fall on a fixed weekly pattern anyway. Each closed
    day or range is entered explicitly, once, by an admin.

    A single-day event sets start_date and end_date to the same date;
    a range (e.g. a two-week break) is one row rather than one row per
    day, which is also what makes "paste a date range" a non-feature —
    the range picker on the form already covers it.

    Read by two very different call sites: the admin management screen
    (core.views.calendar_list for an ADMIN) and the soft-warning check
    on every due-date picker elsewhere in the app (homework due dates,
    fee notice due dates, permission slip response deadlines) via
    core.views.calendar_closed_days_json. Both read the same rows —
    "one calendar data source instead of two" is the point, so a
    future report-card term-dates feature has somewhere to hang off of
    too, without this model needing to change shape for that.
    """

    class EventType(models.TextChoices):
        HOLIDAY = "holiday", "Public holiday"
        IN_SERVICE = "in_service", "In-service day (no students)"
        OTHER = "other", "Other closed day"

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="calendar_events"
    )
    label = models.CharField(max_length=255, help_text="e.g. 'Independence Day' or 'Term 1 break'")
    start_date = models.DateField()
    end_date = models.DateField(
        help_text="Same as start date for a single closed day."
    )
    event_type = models.CharField(
        max_length=10, choices=EventType.choices, default=EventType.OTHER
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="calendar_events_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date"]
        indexes = [
            models.Index(fields=["school", "start_date", "end_date"]),
        ]

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError("End date can't be before the start date.")

    def __str__(self):
        if self.start_date == self.end_date:
            return f"{self.label} ({self.start_date}) — {self.school}"
        return f"{self.label} ({self.start_date} – {self.end_date}) — {self.school}"


class MessageThread(UUIDModel):
    """
    A conversation that shares the Message model below in one of two
    shapes, distinguished by `thread_type`:

    - ONE_TO_ONE: a private conversation between a guardian and a
      teacher, scoped to the one student that connects them — the
      "quick question" channel Announcements and Homework aren't built
      for, since those are always broadcast, never a reply back.
      Exactly one guardian, one teacher, one student per thread. If a
      guardian has two children with two different teachers, that's two
      separate MessageThread rows, not one thread that branches —
      keeping "who can see this thread" trivially answerable (its own
      guardian and teacher fields) for this shape.

    - CLASS: the class-group thread (proposal: "Class Group Messaging")
      — one per SchoolClass, with every guardian currently linked to an
      enrolled student plus the class's homeroom/co-teachers as
      `participants`, kept in sync automatically by core.messaging.
      sync_class_thread as enrollment/teaching-staff changes, rather
      than through the fixed guardian/teacher/student FKs a one-to-one
      thread uses (those three stay null on a class thread).

    guardian and teacher are both plain User FKs (not SchoolMembership)
    on the one-to-one shape, matching how GuardianLink.guardian and
    PermissionSlipResponse.guardian already identify a specific person
    rather than a role assignment — a one-to-one thread is between two
    specific people, not "whoever currently holds the homeroom_teacher
    slot." A class thread's participants list is exactly that kind of
    role-derived membership instead, which is why it needs the M2M and
    the sync helper rather than two fixed FKs.

    Existence of a thread does not by itself mean anyone has actually
    said anything yet — core.views.message_thread_start get_or_creates a
    one-to-one thread with zero messages the moment either side clicks
    "Message" from a student's page, and core.messaging.sync_class_thread
    does the same for a class thread the first time a class with at
    least one enrolled student has messaging turned on. Either kind only
    shows up meaningfully in an inbox once a first Message is sent.
    """

    class ThreadType(models.TextChoices):
        ONE_TO_ONE = "one_to_one", "Guardian ↔ teacher"
        CLASS = "class", "Class group"

    school = models.ForeignKey(
        School, on_delete=models.CASCADE, related_name="message_threads"
    )
    thread_type = models.CharField(
        max_length=10, choices=ThreadType.choices, default=ThreadType.ONE_TO_ONE
    )
    student = models.ForeignKey(
        Student,
        on_delete=models.CASCADE,
        related_name="message_threads",
        null=True,
        blank=True,
        help_text="Set for a one-to-one thread; blank for a class thread, which isn't scoped to a single student.",
    )
    school_class = models.ForeignKey(
        SchoolClass,
        on_delete=models.CASCADE,
        related_name="message_threads",
        null=True,
        blank=True,
        help_text="Set for a class thread; blank for a one-to-one thread.",
    )
    guardian = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="message_threads_as_guardian",
        null=True,
        blank=True,
        help_text="One-to-one threads only.",
    )
    teacher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="message_threads_as_teacher",
        null=True,
        blank=True,
        help_text="One-to-one threads only.",
    )
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="class_message_threads",
        help_text=(
            "Class threads only — every guardian linked to a currently "
            "enrolled, active student in the class, plus its homeroom "
            "and co-teachers. Maintained automatically by "
            "core.messaging.sync_class_thread; never edited directly by "
            "a view. One-to-one threads use guardian/teacher instead and "
            "leave this empty."
        ),
    )
    announcements_only = models.BooleanField(
        default=False,
        help_text=(
            "Class threads only. When on, only the class's teacher(s) "
            "can post — guardians can still read the thread, just not "
            "reply. A teacher-facing toggle for a class where two-way "
            "chat isn't the right fit; off (guardians can post) by "
            "default, matching how a real classroom group usually works."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Denormalized copy of the newest Message.sent_at in this thread, "
            "kept in sync by core.views.message_thread_detail whenever a "
            "reply is posted — lets the inbox sort by most recent activity "
            "without aggregating over every thread's messages on every "
            "inbox load."
        ),
    )

    class Meta:
        ordering = ["-last_message_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["guardian", "teacher", "student"],
                name="unique_thread_per_guardian_teacher_student",
                condition=models.Q(thread_type="one_to_one"),
            ),
            models.UniqueConstraint(
                fields=["school_class"],
                name="unique_thread_per_class",
                condition=models.Q(thread_type="class"),
            ),
        ]
        indexes = [
            models.Index(fields=["school", "guardian"]),
            models.Index(fields=["school", "teacher"]),
            models.Index(fields=["school", "school_class"]),
        ]

    def clean(self):
        if self.thread_type == self.ThreadType.CLASS:
            if not self.school_class_id:
                raise ValidationError("A class thread needs a school_class.")
            if self.student_id or self.guardian_id or self.teacher_id:
                raise ValidationError(
                    "A class thread shouldn't set student/guardian/teacher — use participants."
                )
        else:
            if self.school_class_id or self.announcements_only:
                raise ValidationError(
                    "A one-to-one thread shouldn't set school_class or announcements_only."
                )
            if not (self.student_id and self.guardian_id and self.teacher_id):
                raise ValidationError(
                    "A one-to-one thread needs student, guardian, and teacher."
                )

    def __str__(self):
        if self.thread_type == self.ThreadType.CLASS:
            return f"Class group — {self.school_class}"
        return f"{self.guardian} ↔ {self.teacher} — {self.student}"


class Message(UUIDModel):
    """
    One message within a MessageThread.

    read_at is meaningful without a separate through-model (unlike
    AnnouncementRead) only on a one-to-one thread: it has exactly two
    participants, so there's only ever one possible "other person" who
    could read a given message, and a single nullable timestamp on the
    message itself is enough to answer "has the recipient seen this
    yet." A class thread can have many readers, so it doesn't use this
    field for that purpose at all — see MessageThreadRead, which tracks
    "read up to" per participant instead, and backs that thread type's
    unread badge.

    removed_at/removed_by back class-thread moderation
    (core.views.class_message_remove): a teacher can remove a message,
    but the row stays and the template swaps its body for a visible
    "message removed" placeholder rather than deleting it outright, so
    the rest of the thread doesn't lose context mid-conversation. Not
    used on one-to-one threads, which have no moderation feature.
    """

    thread = models.ForeignKey(MessageThread, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="messages_sent",
    )
    body = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="One-to-one threads only. Set when the other participant views it.",
    )
    removed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Class threads only. Set when the class teacher removes this message.",
    )
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages_removed",
    )

    class Meta:
        ordering = ["sent_at"]
        indexes = [
            models.Index(fields=["thread", "sent_at"]),
        ]

    def __str__(self):
        return f"{self.sender} in {self.thread} at {self.sent_at}"


class MessageThreadRead(UUIDModel):
    """
    Per-participant "read up to" marker for a MessageThread — needed
    once a thread can have more than two participants (class threads),
    where a single Message.read_at (meaningful for a one-to-one thread,
    see that field's docstring) can no longer answer "has this reader
    seen this yet" once there's more than one possible reader. One-to-
    one threads keep using Message.read_at exactly as before; this only
    backs the unread badge/read-state for class threads, compared
    against Message.sent_at rather than tracked message-by-message.
    """

    thread = models.ForeignKey(
        MessageThread, on_delete=models.CASCADE, related_name="read_states"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="thread_read_states"
    )
    last_read_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["thread", "user"], name="unique_thread_read_state"),
        ]

    def __str__(self):
        return f"{self.user} read {self.thread} up to {self.last_read_at}"
