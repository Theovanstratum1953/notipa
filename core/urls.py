from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("offline/", views.offline, name="offline"),
    path("switch-school/", views.switch_school, name="switch_school"),
    path("setup/school/", views.school_setup, name="school_setup"),
    path("wiki/", views.wiki, name="wiki"),
    path("my-children/<uuid:pk>/", views.my_child_detail, name="my_child_detail"),
    path("announcements/", views.announcements_list, name="announcements"),
    path("announcements/new/", views.announcement_new, name="announcement_new"),
    path("announcements/<uuid:pk>/edit/", views.announcement_edit, name="announcement_edit"),
    path(
        "announcements/<uuid:pk>/publish/",
        views.announcement_publish,
        name="announcement_publish",
    ),
    path(
        "announcements/<uuid:pk>/unpublish/",
        views.announcement_unpublish,
        name="announcement_unpublish",
    ),
    path(
        "announcements/<uuid:pk>/delete/", views.announcement_delete, name="announcement_delete"
    ),
    path(
        "announcements/<uuid:pk>/mark-read/",
        views.announcement_mark_read,
        name="announcement_mark_read",
    ),
    path("homework/", views.homework_list, name="homework"),
    path("homework/new/", views.homework_new, name="homework_new"),
    path("homework/<uuid:pk>/", views.homework_detail, name="homework_detail"),
    path("homework/<uuid:pk>/edit/", views.homework_edit, name="homework_edit"),
    path("homework/<uuid:pk>/delete/", views.homework_delete, name="homework_delete"),
    path(
        "homework/<uuid:pk>/students/<uuid:student_pk>/submit/",
        views.homework_submit,
        name="homework_submit",
    ),
    path("fees/", views.fee_notices_list, name="fees"),
    path("fees/new/", views.fee_notice_new, name="fee_notice_new"),
    path("fees/<uuid:pk>/edit/", views.fee_notice_edit, name="fee_notice_edit"),
    path("fees/<uuid:pk>/delete/", views.fee_notice_delete, name="fee_notice_delete"),
    path("fees/<uuid:pk>/mark-paid/", views.fee_notice_mark_paid, name="fee_notice_mark_paid"),
    path(
        "fees/<uuid:pk>/mark-waived/", views.fee_notice_mark_waived, name="fee_notice_mark_waived"
    ),
    path(
        "fees/<uuid:pk>/mark-unpaid/", views.fee_notice_mark_unpaid, name="fee_notice_mark_unpaid"
    ),
    path("permission-slips/", views.permission_slips_list, name="permission_slips"),
    path("permission-slips/new/", views.permission_slip_new, name="permission_slip_new"),
    path(
        "permission-slips/<uuid:pk>/",
        views.permission_slip_detail,
        name="permission_slip_detail",
    ),
    path(
        "permission-slips/<uuid:pk>/edit/",
        views.permission_slip_edit,
        name="permission_slip_edit",
    ),
    path(
        "permission-slips/<uuid:pk>/delete/",
        views.permission_slip_delete,
        name="permission_slip_delete",
    ),
    path(
        "permission-slips/<uuid:pk>/students/<uuid:student_pk>/respond/",
        views.permission_slip_respond,
        name="permission_slip_respond",
    ),
    path("students/", views.students_list, name="students"),
    path("students/new/", views.student_new, name="student_new"),
    path("students/<uuid:pk>/", views.student_detail, name="student_detail"),
    path("students/<uuid:pk>/edit/", views.student_edit, name="student_edit"),
    path("students/<uuid:pk>/archive/", views.student_archive, name="student_archive"),
    path("students/<uuid:pk>/restore/", views.student_restore, name="student_restore"),
    path("students/<uuid:pk>/guardians/add/", views.guardian_link_add, name="guardian_link_add"),
    path(
        "students/<uuid:pk>/guardians/<uuid:link_pk>/remove/",
        views.guardian_link_remove,
        name="guardian_link_remove",
    ),
    path("teachers/", views.teachers_list, name="teachers"),
    path("teachers/new/", views.teacher_new, name="teacher_new"),
    path("teachers/<uuid:pk>/edit/", views.teacher_edit, name="teacher_edit"),
    path("teachers/<uuid:pk>/revoke/", views.teacher_revoke, name="teacher_revoke"),
    path("teachers/<uuid:pk>/restore/", views.teacher_restore, name="teacher_restore"),
    path("guardians/", views.guardians_list, name="guardians"),
    path("guardians/new/", views.guardian_new, name="guardian_new"),
    path("guardians/<uuid:pk>/", views.guardian_detail, name="guardian_detail"),
    path("guardians/<uuid:pk>/edit/", views.guardian_edit, name="guardian_edit"),
    path("guardians/<uuid:pk>/revoke/", views.guardian_revoke, name="guardian_revoke"),
    path("guardians/<uuid:pk>/restore/", views.guardian_restore, name="guardian_restore"),
    path("guardians/<uuid:pk>/students/add/", views.student_link_add, name="student_link_add"),
    path(
        "guardians/<uuid:pk>/students/<uuid:link_pk>/remove/",
        views.student_link_remove,
        name="student_link_remove",
    ),
    path("classes/", views.classes_list, name="classes"),
    path("classes/new/", views.class_new, name="class_new"),
    path("classes/<uuid:pk>/", views.class_detail, name="class_detail"),
    path("classes/<uuid:pk>/edit/", views.class_edit, name="class_edit"),
    path("classes/<uuid:pk>/archive/", views.class_archive, name="class_archive"),
    path("classes/<uuid:pk>/restore/", views.class_restore, name="class_restore"),
    path("settings/", views.school_settings, name="settings"),
]
