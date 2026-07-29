from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

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
    ReportCard,
    ReportCardEntry,
    ReportCardRead,
    School,
    SchoolCalendarEvent,
    SchoolClass,
    SchoolMembership,
    Student,
    StudentRecord,
    Term,
    User,
)


@admin.register(User)
class NotipaUserAdmin(UserAdmin):
    # Adds phone_number/preferred_language onto Django's stock UserAdmin
    # layout rather than replacing it wholesale.
    fieldsets = UserAdmin.fieldsets + (
        ("Notipa profile", {"fields": ("phone_number", "preferred_language")}),
    )
    list_display = ("username", "get_full_name", "email", "phone_number", "is_staff")
    search_fields = ("username", "first_name", "last_name", "email", "phone_number")


class SchoolMembershipInline(admin.TabularInline):
    model = SchoolMembership
    extra = 0
    autocomplete_fields = ["user"]


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "tier", "default_language", "is_active", "created_at")
    list_filter = ("tier", "country", "is_active")
    search_fields = ("name",)
    inlines = [SchoolMembershipInline]


@admin.register(SchoolMembership)
class SchoolMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "school", "role", "is_active", "joined_at")
    list_filter = ("role", "is_active", "school")
    search_fields = ("user__username", "user__first_name", "user__last_name", "school__name")
    autocomplete_fields = ["user", "school"]


@admin.register(SchoolClass)
class SchoolClassAdmin(admin.ModelAdmin):
    list_display = (
        "name", "school", "academic_year", "homeroom_teacher", "messaging_enabled", "is_active",
    )
    list_filter = ("school", "academic_year", "messaging_enabled", "is_active")
    search_fields = ("name",)
    autocomplete_fields = ["school", "homeroom_teacher"]


class GuardianLinkInline(admin.TabularInline):
    model = GuardianLink
    extra = 0
    autocomplete_fields = ["guardian"]


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ("last_name", "first_name", "school", "school_class", "student_id", "is_active")
    list_filter = ("school", "school_class", "is_active")
    search_fields = ("first_name", "last_name", "student_id")
    autocomplete_fields = ["school", "school_class"]
    inlines = [GuardianLinkInline]


@admin.register(GuardianLink)
class GuardianLinkAdmin(admin.ModelAdmin):
    list_display = ("guardian", "student", "relationship", "is_primary_contact")
    list_filter = ("relationship", "is_primary_contact")
    search_fields = ("guardian__username", "guardian__first_name", "guardian__last_name",
                      "student__first_name", "student__last_name")
    autocomplete_fields = ["guardian", "student"]


class AnnouncementReadInline(admin.TabularInline):
    model = AnnouncementRead
    extra = 0
    autocomplete_fields = ["guardian"]


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "school", "school_class", "author", "published_at", "created_at")
    list_filter = ("school", "school_class")
    search_fields = ("title", "body")
    autocomplete_fields = ["school", "school_class", "author"]
    inlines = [AnnouncementReadInline]


class HomeworkSubmissionInline(admin.TabularInline):
    model = HomeworkSubmission
    extra = 0
    autocomplete_fields = ["student", "submitted_by"]


@admin.register(Homework)
class HomeworkAdmin(admin.ModelAdmin):
    list_display = (
        "title", "school_class", "due_date", "accepts_submissions", "created_by", "created_at",
    )
    list_filter = ("school_class", "accepts_submissions")
    search_fields = ("title", "description")
    autocomplete_fields = ["school_class", "created_by"]
    inlines = [HomeworkSubmissionInline]


@admin.register(HomeworkSubmission)
class HomeworkSubmissionAdmin(admin.ModelAdmin):
    list_display = ("homework", "student", "status", "submitted_by", "submitted_at")
    list_filter = ("status",)
    search_fields = ("student__first_name", "student__last_name", "homework__title")
    autocomplete_fields = ["homework", "student", "submitted_by"]


@admin.register(FeeNotice)
class FeeNoticeAdmin(admin.ModelAdmin):
    list_display = ("title", "student", "school", "amount", "currency", "due_date", "status")
    list_filter = ("school", "status", "currency")
    search_fields = ("title", "student__first_name", "student__last_name")
    autocomplete_fields = ["school", "student", "created_by"]


class PermissionSlipResponseInline(admin.TabularInline):
    model = PermissionSlipResponse
    extra = 0
    autocomplete_fields = ["student", "guardian"]


@admin.register(PermissionSlip)
class PermissionSlipAdmin(admin.ModelAdmin):
    list_display = ("title", "school", "school_class", "event_date", "response_deadline")
    list_filter = ("school", "school_class")
    search_fields = ("title", "description")
    autocomplete_fields = ["school", "school_class", "created_by"]
    inlines = [PermissionSlipResponseInline]


@admin.register(PermissionSlipResponse)
class PermissionSlipResponseAdmin(admin.ModelAdmin):
    list_display = ("permission_slip", "student", "guardian", "response", "responded_at")
    list_filter = ("response",)
    search_fields = ("student__first_name", "student__last_name", "guardian__username")
    autocomplete_fields = ["permission_slip", "student", "guardian"]


@admin.register(StudentRecord)
class StudentRecordAdmin(admin.ModelAdmin):
    list_display = ("title", "student", "school", "author", "visible_to_guardians", "created_at")
    list_filter = ("school", "visible_to_guardians")
    search_fields = ("title", "body", "student__first_name", "student__last_name")
    autocomplete_fields = ["school", "student", "author"]


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("student", "school_class", "date", "status", "recorded_by", "last_edited_at")
    list_filter = ("school_class", "status")
    search_fields = ("student__first_name", "student__last_name", "school_class__name")
    autocomplete_fields = ["student", "school_class", "recorded_by"]


@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ("name", "school", "start_date", "end_date")
    list_filter = ("school",)
    search_fields = ("name",)
    autocomplete_fields = ["school"]


class ReportCardEntryInline(admin.TabularInline):
    model = ReportCardEntry
    extra = 0


class ReportCardReadInline(admin.TabularInline):
    model = ReportCardRead
    extra = 0
    autocomplete_fields = ["guardian"]


@admin.register(ReportCard)
class ReportCardAdmin(admin.ModelAdmin):
    list_display = ("student", "term", "school_class", "status", "published_at", "created_by")
    list_filter = ("term", "status", "school_class")
    search_fields = ("student__first_name", "student__last_name")
    autocomplete_fields = ["student", "term", "school_class", "created_by"]
    inlines = [ReportCardEntryInline, ReportCardReadInline]


@admin.register(SchoolCalendarEvent)
class SchoolCalendarEventAdmin(admin.ModelAdmin):
    list_display = ("label", "school", "start_date", "end_date", "event_type", "created_by")
    list_filter = ("school", "event_type")
    search_fields = ("label",)
    autocomplete_fields = ["school", "created_by"]


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    autocomplete_fields = ["sender", "removed_by"]


class MessageThreadReadInline(admin.TabularInline):
    model = MessageThreadRead
    extra = 0
    autocomplete_fields = ["user"]


@admin.register(MessageThread)
class MessageThreadAdmin(admin.ModelAdmin):
    list_display = (
        "__str__", "thread_type", "school_class", "student", "guardian", "teacher",
        "announcements_only", "school", "last_message_at", "created_at",
    )
    list_filter = ("school", "thread_type", "announcements_only")
    search_fields = (
        "student__first_name", "student__last_name",
        "guardian__username", "teacher__username",
        "school_class__name",
    )
    autocomplete_fields = ["school", "student", "school_class", "guardian", "teacher", "participants"]
    inlines = [MessageInline, MessageThreadReadInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("thread", "sender", "sent_at", "read_at", "removed_at", "removed_by")
    list_filter = ("sent_at", "removed_at")
    search_fields = ("body",)
    autocomplete_fields = ["thread", "sender", "removed_by"]
