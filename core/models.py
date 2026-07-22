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
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


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
    attachment = models.FileField(upload_to="homework/%Y/%m/", blank=True, null=True)
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
