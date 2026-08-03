import shutil
import tempfile
from datetime import date, datetime, timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import SchoolClassForm
from .messaging import class_messaging_effectively_enabled, thread_messaging_enabled
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
from .push import push_configured, send_push_notification
from .views import _sync_permission_slip_responses

User = get_user_model()


class TeacherInviteTests(TestCase):
    """
    Covers the in-app teacher/admin invite flow (core.views.teacher_new /
    teachers_list, core.forms.TeacherForm) — the onboarding gap flagged in
    the handover plan (Section 10 / Section 9 item 2). Mirrors the
    school-scoping tests already written for Classes/Students: an admin
    can add staff to their own school, the new membership is scoped
    correctly, a non-admin can't reach the flow, and one school's admin
    can't see another school's staff.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )

        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )

        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

    def _teacher_payload(self, **overrides):
        payload = {
            "first_name": "Maria",
            "last_name": "Santos",
            "username": "maria_santos",
            "email": "",
            "phone_number": "+639171234567",
            "role": SchoolMembership.Role.TEACHER,
            "password1": "a-strong-password-1",
            "password2": "a-strong-password-1",
        }
        payload.update(overrides)
        return payload

    def test_admin_can_create_teacher(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:teacher_new"), self._teacher_payload())
        self.assertRedirects(response, reverse("core:teachers"))

        new_user = User.objects.get(username="maria_santos")
        self.assertTrue(new_user.check_password("a-strong-password-1"))
        membership = SchoolMembership.objects.get(user=new_user)
        self.assertEqual(membership.school, self.school_a)
        self.assertEqual(membership.role, SchoolMembership.Role.TEACHER)

    def test_new_teacher_can_log_in(self):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(reverse("core:teacher_new"), self._teacher_payload())
        self.client.logout()
        self.assertTrue(
            self.client.login(username="maria_santos", password="a-strong-password-1")
        )

    def test_mismatched_passwords_rejected(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:teacher_new"),
            self._teacher_payload(password2="something-else"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="maria_santos").exists())

    def test_duplicate_username_rejected(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:teacher_new"), self._teacher_payload(username="teacher_a")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="teacher_a").count(), 1)

    def test_teacher_cannot_create_teacher(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:teacher_new"))
        self.assertEqual(response.status_code, 403)

    def test_teacher_cannot_view_teachers_list(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:teachers"))
        self.assertEqual(response.status_code, 403)

    def test_admin_only_sees_own_school_staff(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:teachers"))
        memberships = list(response.context["memberships"])
        self.assertIn(self.admin_a.memberships.get(school=self.school_a), memberships)
        self.assertNotIn(
            self.admin_b.memberships.get(school=self.school_b), memberships
        )

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("core:teacher_new"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class TeacherEditRevokeTests(TestCase):
    """
    Covers core.views.teacher_edit/teacher_revoke/teacher_restore and
    core.forms.TeacherEditForm — editing an existing teacher's details
    and soft-deleting (revoking) their SchoolMembership without touching
    the underlying User or their history.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )

        self.teacher_user = User.objects.create_user(
            username="teacher_a", password="pw12345!", first_name="Juan", last_name="Cruz"
        )
        self.teacher_membership = SchoolMembership.objects.create(
            user=self.teacher_user, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )

        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        self.membership_b = SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

    def _edit_payload(self, **overrides):
        payload = {
            "first_name": "Juana",
            "last_name": "Cruz",
            "email": "",
            "phone_number": "",
            "role": SchoolMembership.Role.TEACHER,
        }
        payload.update(overrides)
        return payload

    def test_admin_can_edit_teacher(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:teacher_edit", args=[self.teacher_membership.pk]),
            self._edit_payload(role=SchoolMembership.Role.ADMIN),
        )
        self.assertRedirects(response, reverse("core:teachers"))
        self.teacher_user.refresh_from_db()
        self.teacher_membership.refresh_from_db()
        self.assertEqual(self.teacher_user.first_name, "Juana")
        self.assertEqual(self.teacher_membership.role, SchoolMembership.Role.ADMIN)

    def test_role_conflict_rejected(self):
        # Give the same user a second membership at the same school with
        # the ADMIN role, then try to edit the TEACHER one to also be ADMIN.
        SchoolMembership.objects.create(
            user=self.teacher_user, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:teacher_edit", args=[self.teacher_membership.pk]),
            self._edit_payload(role=SchoolMembership.Role.ADMIN),
        )
        self.assertEqual(response.status_code, 200)
        self.teacher_membership.refresh_from_db()
        self.assertEqual(self.teacher_membership.role, SchoolMembership.Role.TEACHER)

    def test_admin_cannot_edit_other_school_membership(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:teacher_edit", args=[self.membership_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_revoke_and_restore(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:teacher_revoke", args=[self.teacher_membership.pk])
        )
        self.assertRedirects(response, reverse("core:teachers"))
        self.teacher_membership.refresh_from_db()
        self.assertFalse(self.teacher_membership.is_active)

        # Revoked accounts can no longer act as that role (middleware only
        # attaches *active* memberships) — confirmed by teachers_list still
        # returning 403 for them.
        self.client.logout()
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:teachers"))
        self.assertEqual(response.status_code, 403)

        self.client.logout()
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:teacher_restore", args=[self.teacher_membership.pk])
        )
        self.assertRedirects(response, reverse("core:teachers"))
        self.teacher_membership.refresh_from_db()
        self.assertTrue(self.teacher_membership.is_active)


class ClassEditArchiveTests(TestCase):
    """Covers core.views.class_edit/class_archive/class_restore."""

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school_b, name="Grade 5 - Rosal", academic_year="2026-2027"
        )

    def test_admin_can_edit_class(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:class_edit", args=[self.class_a.pk]),
            {"name": "Grade 4 - Ilang-Ilang", "academic_year": "2026-2027", "homeroom_teacher": ""},
        )
        self.assertRedirects(response, reverse("core:classes"))
        self.class_a.refresh_from_db()
        self.assertEqual(self.class_a.name, "Grade 4 - Ilang-Ilang")

    def test_cannot_edit_other_school_class(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:class_edit", args=[self.class_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_archive_hides_from_default_list_but_restore_brings_back(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:class_archive", args=[self.class_a.pk]))
        self.assertRedirects(response, reverse("core:classes"))
        self.class_a.refresh_from_db()
        self.assertFalse(self.class_a.is_active)

        response = self.client.get(reverse("core:classes"))
        self.assertNotIn(self.class_a, list(response.context["classes"]))

        response = self.client.get(reverse("core:classes") + "?show=archived")
        self.assertIn(self.class_a, list(response.context["classes"]))

        response = self.client.post(reverse("core:class_restore", args=[self.class_a.pk]))
        self.assertRedirects(response, reverse("core:classes"))
        self.class_a.refresh_from_db()
        self.assertTrue(self.class_a.is_active)

    def test_archived_class_excluded_from_student_form_dropdown(self):
        self.class_a.is_active = False
        self.class_a.save(update_fields=["is_active"])
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:student_new"))
        self.assertNotIn(self.class_a, list(response.context["form"].fields["school_class"].queryset))


class StudentEditArchiveTests(TestCase):
    """Covers core.views.student_edit/student_archive/student_restore."""

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.student_a = Student.objects.create(
            school=self.school_a, first_name="Ana", last_name="Reyes"
        )
        self.student_b = Student.objects.create(
            school=self.school_b, first_name="Ben", last_name="Torres"
        )

    def test_admin_can_edit_student(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:student_edit", args=[self.student_a.pk]),
            {
                "first_name": "Ana",
                "last_name": "Villanueva",
                "date_of_birth": "",
                "student_id": "",
                "school_class": "",
            },
        )
        self.assertRedirects(response, reverse("core:students"))
        self.student_a.refresh_from_db()
        self.assertEqual(self.student_a.last_name, "Villanueva")

    def test_cannot_edit_other_school_student(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:student_edit", args=[self.student_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_archive_hides_from_default_list_but_restore_brings_back(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:student_archive", args=[self.student_a.pk]))
        self.assertRedirects(response, reverse("core:students"))
        self.student_a.refresh_from_db()
        self.assertFalse(self.student_a.is_active)

        response = self.client.get(reverse("core:students"))
        self.assertNotIn(self.student_a, list(response.context["students"]))

        response = self.client.get(reverse("core:students") + "?show=archived")
        self.assertIn(self.student_a, list(response.context["students"]))

        response = self.client.post(reverse("core:student_restore", args=[self.student_a.pk]))
        self.assertRedirects(response, reverse("core:students"))
        self.student_a.refresh_from_db()
        self.assertTrue(self.student_a.is_active)


class ClassDetailTests(TestCase):
    """
    Covers core.views.class_detail — the single-class view showing the
    homeroom (+ co-)teacher and the class roster, linked from
    classes_list and students_list.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.teacher_user = User.objects.create_user(
            username="teacher_a", password="pw12345!", first_name="Juan", last_name="Cruz"
        )
        self.teacher_membership = SchoolMembership.objects.create(
            user=self.teacher_user, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a,
            name="Grade 4 - Sampaguita",
            academic_year="2026-2027",
            homeroom_teacher=self.teacher_membership,
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school_b, name="Grade 5 - Rosal", academic_year="2026-2027"
        )

        self.active_student = Student.objects.create(
            school=self.school_a,
            school_class=self.class_a,
            first_name="Ana",
            last_name="Reyes",
        )
        self.archived_student = Student.objects.create(
            school=self.school_a,
            school_class=self.class_a,
            first_name="Ben",
            last_name="Santos",
            is_active=False,
        )

    def test_shows_homeroom_teacher_and_active_students(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:class_detail", args=[self.class_a.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["school_class"], self.class_a)
        students = list(response.context["students"])
        self.assertIn(self.active_student, students)
        self.assertNotIn(self.archived_student, students)
        self.assertContains(response, "Juan Cruz")

    def test_show_archived_includes_archived_students(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(
            reverse("core:class_detail", args=[self.class_a.pk]) + "?show=archived"
        )
        students = list(response.context["students"])
        self.assertIn(self.active_student, students)
        self.assertIn(self.archived_student, students)

    def test_cannot_view_other_school_class(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:class_detail", args=[self.class_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_guardian_cannot_view_class_detail(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:class_detail", args=[self.class_a.pk]))
        self.assertEqual(response.status_code, 403)


class GuardianInviteTests(TestCase):
    """
    Covers the in-app guardian invite flow (core.views.guardian_new /
    guardians_list, core.forms.GuardianForm) — the prerequisite for
    linking a guardian to a Student (core.models.GuardianLink). Unlike
    teacher_new, this is admin *or* teacher (routine roster work, not a
    staff-privilege action), mirroring Classes/Students.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

    def _guardian_payload(self, **overrides):
        payload = {
            "first_name": "Rosa",
            "last_name": "Dela Cruz",
            "username": "rosa_delacruz",
            "phone_number": "+639171234567",
            "email": "",
            "password1": "a-strong-password-1",
            "password2": "a-strong-password-1",
        }
        payload.update(overrides)
        return payload

    def test_admin_can_create_guardian(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:guardian_new"), self._guardian_payload())
        self.assertRedirects(response, reverse("core:guardians"))

        new_user = User.objects.get(username="rosa_delacruz")
        self.assertTrue(new_user.check_password("a-strong-password-1"))
        self.assertEqual(new_user.phone_number, "+639171234567")
        membership = SchoolMembership.objects.get(user=new_user)
        self.assertEqual(membership.school, self.school_a)
        self.assertEqual(membership.role, SchoolMembership.Role.GUARDIAN)

    def test_teacher_can_also_create_guardian(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(reverse("core:guardian_new"), self._guardian_payload())
        self.assertRedirects(response, reverse("core:guardians"))
        self.assertTrue(User.objects.filter(username="rosa_delacruz").exists())

    def test_phone_number_required(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:guardian_new"), self._guardian_payload(phone_number="")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="rosa_delacruz").exists())

    def test_duplicate_username_rejected(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:guardian_new"), self._guardian_payload(username="guardian_a")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(username="guardian_a").count(), 1)

    def test_new_guardian_can_log_in(self):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(reverse("core:guardian_new"), self._guardian_payload())
        self.client.logout()
        self.assertTrue(
            self.client.login(username="rosa_delacruz", password="a-strong-password-1")
        )

    def test_guardian_cannot_create_guardian(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:guardian_new"))
        self.assertEqual(response.status_code, 403)

    def test_admin_only_sees_own_school_guardians(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:guardians"))
        memberships = list(response.context["memberships"])
        self.assertIn(self.guardian_a.memberships.get(school=self.school_a), memberships)


class GuardianEditRevokeTests(TestCase):
    """Covers core.views.guardian_edit/guardian_revoke/guardian_restore."""

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )

        self.guardian_user = User.objects.create_user(
            username="guardian_a",
            password="pw12345!",
            first_name="Rosa",
            last_name="Dela Cruz",
        )
        self.guardian_membership = SchoolMembership.objects.create(
            user=self.guardian_user,
            school=self.school_a,
            role=SchoolMembership.Role.GUARDIAN,
        )

        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        self.membership_b = SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

    def test_admin_can_edit_guardian(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:guardian_edit", args=[self.guardian_membership.pk]),
            {
                "first_name": "Rosario",
                "last_name": "Dela Cruz",
                "phone_number": "+639179998888",
                "email": "",
            },
        )
        self.assertRedirects(response, reverse("core:guardians"))
        self.guardian_user.refresh_from_db()
        self.assertEqual(self.guardian_user.first_name, "Rosario")
        self.assertEqual(self.guardian_user.phone_number, "+639179998888")

    def test_admin_cannot_edit_other_school_guardian_membership(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:guardian_edit", args=[self.membership_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_revoke_and_restore(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:guardian_revoke", args=[self.guardian_membership.pk])
        )
        self.assertRedirects(response, reverse("core:guardians"))
        self.guardian_membership.refresh_from_db()
        self.assertFalse(self.guardian_membership.is_active)

        response = self.client.post(
            reverse("core:guardian_restore", args=[self.guardian_membership.pk])
        )
        self.assertRedirects(response, reverse("core:guardians"))
        self.guardian_membership.refresh_from_db()
        self.assertTrue(self.guardian_membership.is_active)


class GuardianLinkTests(TestCase):
    """
    Covers core.views.student_detail/guardian_link_add/guardian_link_remove
    and core.forms.GuardianLinkForm — actually attaching a guardian
    account (Section 19) to a Student via core.models.GuardianLink. This
    is the piece that makes Guardian setup mean anything.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )

        self.guardian_1 = User.objects.create_user(
            username="guardian_1", password="pw12345!", first_name="Rosa", last_name="Reyes"
        )
        self.guardian_1_membership = SchoolMembership.objects.create(
            user=self.guardian_1, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.guardian_2 = User.objects.create_user(
            username="guardian_2", password="pw12345!", first_name="Pedro", last_name="Reyes"
        )
        SchoolMembership.objects.create(
            user=self.guardian_2, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        # A guardian at School B — must never show up in School A's dropdown.
        self.guardian_other_school = User.objects.create_user(
            username="guardian_other", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.guardian_other_school, school=self.school_b, role=SchoolMembership.Role.GUARDIAN
        )
        # A revoked guardian at School A — must not show up either.
        self.guardian_revoked = User.objects.create_user(
            username="guardian_revoked", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.guardian_revoked,
            school=self.school_a,
            role=SchoolMembership.Role.GUARDIAN,
            is_active=False,
        )

        self.student_a = Student.objects.create(
            school=self.school_a, first_name="Ana", last_name="Reyes"
        )
        self.student_b = Student.objects.create(
            school=self.school_b, first_name="Ben", last_name="Torres"
        )

    def test_dropdown_scoped_to_school_and_excludes_revoked(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:student_detail", args=[self.student_a.pk]))
        eligible = list(response.context["form"].fields["guardian"].queryset)
        self.assertIn(self.guardian_1, eligible)
        self.assertIn(self.guardian_2, eligible)
        self.assertNotIn(self.guardian_other_school, eligible)
        self.assertNotIn(self.guardian_revoked, eligible)

    def test_admin_can_link_guardian(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:guardian_link_add", args=[self.student_a.pk]),
            {
                "guardian": str(self.guardian_1.pk),
                "relationship": GuardianLink.Relationship.MOTHER,
                "is_primary_contact": "on",
            },
        )
        self.assertRedirects(response, reverse("core:student_detail", args=[self.student_a.pk]))
        link = GuardianLink.objects.get(guardian=self.guardian_1, student=self.student_a)
        self.assertEqual(link.relationship, GuardianLink.Relationship.MOTHER)
        self.assertTrue(link.is_primary_contact)

    def test_only_one_primary_contact_per_student(self):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(
            reverse("core:guardian_link_add", args=[self.student_a.pk]),
            {
                "guardian": str(self.guardian_1.pk),
                "relationship": GuardianLink.Relationship.MOTHER,
                "is_primary_contact": "on",
            },
        )
        self.client.post(
            reverse("core:guardian_link_add", args=[self.student_a.pk]),
            {
                "guardian": str(self.guardian_2.pk),
                "relationship": GuardianLink.Relationship.FATHER,
                "is_primary_contact": "on",
            },
        )
        links = GuardianLink.objects.filter(student=self.student_a)
        self.assertEqual(links.filter(is_primary_contact=True).count(), 1)
        self.assertTrue(
            links.get(guardian=self.guardian_2).is_primary_contact
        )
        self.assertFalse(links.get(guardian=self.guardian_1).is_primary_contact)

    def test_already_linked_guardian_excluded_from_dropdown_and_rejected(self):
        GuardianLink.objects.create(
            guardian=self.guardian_1,
            student=self.student_a,
            relationship=GuardianLink.Relationship.MOTHER,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:student_detail", args=[self.student_a.pk]))
        eligible = list(response.context["form"].fields["guardian"].queryset)
        self.assertNotIn(self.guardian_1, eligible)

        # Even a bypassed POST (e.g. tampered form) is rejected, not a
        # duplicate row or an IntegrityError.
        response = self.client.post(
            reverse("core:guardian_link_add", args=[self.student_a.pk]),
            {
                "guardian": str(self.guardian_1.pk),
                "relationship": GuardianLink.Relationship.FATHER,
                "is_primary_contact": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            GuardianLink.objects.filter(guardian=self.guardian_1, student=self.student_a).count(),
            1,
        )

    def test_cannot_view_other_school_student_detail(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:student_detail", args=[self.student_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_remove_link(self):
        link = GuardianLink.objects.create(
            guardian=self.guardian_1,
            student=self.student_a,
            relationship=GuardianLink.Relationship.MOTHER,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:guardian_link_remove", args=[self.student_a.pk, link.pk])
        )
        self.assertRedirects(response, reverse("core:student_detail", args=[self.student_a.pk]))
        self.assertFalse(GuardianLink.objects.filter(pk=link.pk).exists())
        # The guardian account itself is untouched.
        self.assertTrue(User.objects.filter(pk=self.guardian_1.pk).exists())
        self.assertTrue(
            SchoolMembership.objects.filter(pk=self.guardian_1_membership.pk).exists()
        )

    def test_cannot_remove_link_via_other_school_student(self):
        link = GuardianLink.objects.create(
            guardian=self.guardian_1,
            student=self.student_a,
            relationship=GuardianLink.Relationship.MOTHER,
        )
        self.client.login(username="admin_a", password="pw12345!")
        # student_b belongs to school_b, this admin has no access there —
        # confirm the link can't be removed by pairing it with the wrong
        # (inaccessible) student id.
        response = self.client.post(
            reverse("core:guardian_link_remove", args=[self.student_b.pk, link.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(GuardianLink.objects.filter(pk=link.pk).exists())


class StudentLinkTests(TestCase):
    """
    Covers core.views.guardian_detail/student_link_add/student_link_remove
    and core.forms.StudentLinkForm — the mirror of GuardianLinkTests,
    starting from the guardian's own page instead of the student's, for
    the "add several siblings to one guardian" case.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )

        self.guardian = User.objects.create_user(
            username="guardian_1", password="pw12345!", first_name="Rosa", last_name="Reyes"
        )
        self.guardian_membership = SchoolMembership.objects.create(
            user=self.guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.child_1 = Student.objects.create(
            school=self.school_a, first_name="Ana", last_name="Reyes"
        )
        self.child_2 = Student.objects.create(
            school=self.school_a, first_name="Ben", last_name="Reyes"
        )
        self.archived_child = Student.objects.create(
            school=self.school_a, first_name="Cara", last_name="Reyes", is_active=False
        )
        self.other_school_student = Student.objects.create(
            school=self.school_b, first_name="Deo", last_name="Santos"
        )

        # A different guardian, different school, for isolation checks.
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        self.other_guardian = User.objects.create_user(username="guardian_b", password="pw12345!")
        self.other_guardian_membership = SchoolMembership.objects.create(
            user=self.other_guardian, school=self.school_b, role=SchoolMembership.Role.GUARDIAN
        )
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

    def test_dropdown_excludes_archived_and_other_school_students(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(
            reverse("core:guardian_detail", args=[self.guardian_membership.pk])
        )
        eligible = list(response.context["form"].fields["student"].queryset)
        self.assertIn(self.child_1, eligible)
        self.assertIn(self.child_2, eligible)
        self.assertNotIn(self.archived_child, eligible)
        self.assertNotIn(self.other_school_student, eligible)

    def test_can_link_multiple_children_to_one_guardian(self):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(
            reverse("core:student_link_add", args=[self.guardian_membership.pk]),
            {
                "student": str(self.child_1.pk),
                "relationship": GuardianLink.Relationship.MOTHER,
                "is_primary_contact": "on",
            },
        )
        response = self.client.post(
            reverse("core:student_link_add", args=[self.guardian_membership.pk]),
            {
                "student": str(self.child_2.pk),
                "relationship": GuardianLink.Relationship.MOTHER,
                "is_primary_contact": "on",
            },
        )
        self.assertRedirects(
            response, reverse("core:guardian_detail", args=[self.guardian_membership.pk])
        )
        links = GuardianLink.objects.filter(guardian=self.guardian)
        self.assertEqual(links.count(), 2)
        self.assertIn(self.child_1, [link.student for link in links])
        self.assertIn(self.child_2, [link.student for link in links])

    def test_already_linked_student_excluded_and_rejected(self):
        GuardianLink.objects.create(
            guardian=self.guardian, student=self.child_1, relationship=GuardianLink.Relationship.MOTHER
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(
            reverse("core:guardian_detail", args=[self.guardian_membership.pk])
        )
        eligible = list(response.context["form"].fields["student"].queryset)
        self.assertNotIn(self.child_1, eligible)

        response = self.client.post(
            reverse("core:student_link_add", args=[self.guardian_membership.pk]),
            {
                "student": str(self.child_1.pk),
                "relationship": GuardianLink.Relationship.FATHER,
                "is_primary_contact": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            GuardianLink.objects.filter(guardian=self.guardian, student=self.child_1).count(), 1
        )

    def test_cannot_view_other_school_guardian_detail(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(
            reverse("core:guardian_detail", args=[self.other_guardian_membership.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_remove_link_from_guardian_side(self):
        link = GuardianLink.objects.create(
            guardian=self.guardian, student=self.child_1, relationship=GuardianLink.Relationship.MOTHER
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:student_link_remove", args=[self.guardian_membership.pk, link.pk])
        )
        self.assertRedirects(
            response, reverse("core:guardian_detail", args=[self.guardian_membership.pk])
        )
        self.assertFalse(GuardianLink.objects.filter(pk=link.pk).exists())
        # Neither the guardian's nor the student's own record is touched.
        self.assertTrue(User.objects.filter(pk=self.guardian.pk).exists())
        self.assertTrue(Student.objects.filter(pk=self.child_1.pk).exists())

    def test_cannot_remove_link_via_other_guardian(self):
        link = GuardianLink.objects.create(
            guardian=self.guardian, student=self.child_1, relationship=GuardianLink.Relationship.MOTHER
        )
        self.client.login(username="admin_a", password="pw12345!")
        # Pair a real link id with a membership pk it doesn't actually
        # belong to (a different guardian's) — should 404, not remove it.
        response = self.client.post(
            reverse(
                "core:student_link_remove",
                args=[self.other_guardian_membership.pk, link.pk],
            )
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(GuardianLink.objects.filter(pk=link.pk).exists())

    def test_linking_from_either_side_produces_same_record(self):
        """Linking via guardian_detail (student_link_add) and linking via
        student_detail (guardian_link_add) both just create a GuardianLink
        — confirms the two entry points are genuinely symmetric."""
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(
            reverse("core:student_link_add", args=[self.guardian_membership.pk]),
            {
                "student": str(self.child_1.pk),
                "relationship": GuardianLink.Relationship.MOTHER,
                "is_primary_contact": "",
            },
        )
        response = self.client.get(reverse("core:student_detail", args=[self.child_1.pk]))
        guardian_links = list(response.context["guardian_links"])
        self.assertEqual(len(guardian_links), 1)
        self.assertEqual(guardian_links[0].guardian, self.guardian)


class WikiTests(TestCase):
    """
    Covers core.views.wiki — the in-app user manual. Unlike every other
    real view here, it's deliberately not school-scoped and not
    role-restricted (just @login_required), since there's nothing in
    static documentation that needs hiding from any particular role.
    """

    def setUp(self):
        self.school = School.objects.create(name="School A", country="PH")

        self.admin = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin, school=self.school, role=SchoolMembership.Role.ADMIN
        )
        self.teacher = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher, school=self.school, role=SchoolMembership.Role.TEACHER
        )
        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        self.orphan = User.objects.create_user(username="orphan", password="pw12345!")

    def test_admin_can_view_wiki(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:wiki"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "User Manual")

    def test_teacher_can_view_wiki(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:wiki"))
        self.assertEqual(response.status_code, 200)

    def test_guardian_can_view_wiki(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:wiki"))
        self.assertEqual(response.status_code, 200)

    def test_user_with_no_school_can_view_wiki(self):
        self.client.login(username="orphan", password="pw12345!")
        response = self.client.get(reverse("core:wiki"))
        self.assertEqual(response.status_code, 200)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("core:wiki"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class GuardianDashboardTests(TestCase):
    """
    Covers core.views.dashboard's guardian branch (_guardian_dashboard)
    and core.views.my_child_detail — the first guardian-facing views.
    A guardian sees their own children on the dashboard instead of the
    admin/teacher school-wide counts, and can view (but not edit) each
    child's basic info.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )

        self.teacher_user = User.objects.create_user(
            username="teacher_a", password="pw12345!", first_name="Juan", last_name="Cruz"
        )
        self.teacher_membership = SchoolMembership.objects.create(
            user=self.teacher_user, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.class_a = SchoolClass.objects.create(
            school=self.school_a,
            name="Grade 4 - Sampaguita",
            academic_year="2026-2027",
            homeroom_teacher=self.teacher_membership,
        )

        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        # A second guardian at the same school, to confirm isolation.
        self.other_guardian = User.objects.create_user(username="guardian_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.other_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.own_child = Student.objects.create(
            school=self.school_a,
            school_class=self.class_a,
            first_name="Ana",
            last_name="Reyes",
        )
        self.other_child = Student.objects.create(
            school=self.school_a, first_name="Ben", last_name="Santos"
        )
        GuardianLink.objects.create(
            guardian=self.guardian,
            student=self.own_child,
            relationship=GuardianLink.Relationship.MOTHER,
            is_primary_contact=True,
        )
        GuardianLink.objects.create(
            guardian=self.other_guardian,
            student=self.other_child,
            relationship=GuardianLink.Relationship.MOTHER,
        )

    def test_admin_still_sees_ordinary_dashboard(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertTemplateUsed(response, "core/dashboard.html")
        self.assertIn("student_count", response.context)

    def test_guardian_sees_guardian_dashboard_with_only_own_children(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertTemplateUsed(response, "core/guardian_dashboard.html")
        links = list(response.context["guardian_links"])
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].student, self.own_child)
        self.assertContains(response, "Ana Reyes")
        self.assertNotContains(response, "Ben Santos")

    def test_guardian_with_no_linked_children_sees_empty_state(self):
        lonely_guardian = User.objects.create_user(username="guardian_c", password="pw12345!")
        SchoolMembership.objects.create(
            user=lonely_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.client.login(username="guardian_c", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(list(response.context["guardian_links"]), [])
        self.assertContains(response, "No children are linked")

    def test_guardian_can_view_own_child(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_detail", args=[self.own_child.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Juan Cruz")

    def test_guardian_cannot_view_unrelated_child(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_detail", args=[self.other_child.pk]))
        self.assertEqual(response.status_code, 404)

    def test_admin_cannot_use_my_child_detail(self):
        # my_child_detail is guardian-only, even for a student the admin
        # can otherwise see via student_detail.
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_detail", args=[self.own_child.pk]))
        self.assertEqual(response.status_code, 403)

    def test_people_nav_hidden_for_guardian(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(response, reverse("core:students"))
        self.assertNotContains(response, reverse("core:classes"))

    def test_people_nav_visible_for_admin(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertContains(response, reverse("core:students"))
        self.assertContains(response, reverse("core:classes"))

    def test_admin_nav_section_hidden_for_guardian(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(response, reverse("core:settings"))
        self.assertNotContains(response, ">Admin<")

    def test_admin_nav_section_hidden_for_teacher(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertNotContains(response, reverse("core:settings"))

    def test_admin_nav_section_visible_for_admin(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertContains(response, reverse("core:settings"))

    def test_school_setup_link_visible_for_superuser_with_no_membership(self):
        superuser = User.objects.create_user(
            username="root", password="pw12345!", is_superuser=True, is_staff=True
        )
        self.client.login(username="root", password="pw12345!")
        response = self.client.get(reverse("core:dashboard"))
        self.assertContains(response, reverse("core:school_setup"))


class AnnouncementCRUDTests(TestCase):
    """
    Covers the admin/teacher side of Announcements (Phase 1 build
    sequence item 4): core.views.announcements_list/announcement_new/
    announcement_edit/announcement_publish/announcement_unpublish/
    announcement_delete, and core.forms.AnnouncementForm.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )

    def test_new_announcement_saves_as_draft(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:announcement_new"),
            {"title": "Sports Day", "body": "Bring water bottles.", "school_class": ""},
        )
        self.assertRedirects(response, reverse("core:announcements"))
        announcement = Announcement.objects.get(title="Sports Day")
        self.assertIsNone(announcement.published_at)
        self.assertEqual(announcement.author, self.admin_a)
        self.assertEqual(announcement.school, self.school_a)
        self.assertIsNone(announcement.school_class)

    def test_teacher_can_create_class_scoped_announcement(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:announcement_new"),
            {
                "title": "Field trip",
                "body": "Permission slips due Friday.",
                "school_class": str(self.class_a.pk),
            },
        )
        self.assertRedirects(response, reverse("core:announcements"))
        announcement = Announcement.objects.get(title="Field trip")
        self.assertEqual(announcement.school_class, self.class_a)

    def test_guardian_cannot_create_announcement(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:announcement_new"))
        self.assertEqual(response.status_code, 403)

    def test_publish_and_unpublish(self):
        announcement = Announcement.objects.create(
            school=self.school_a, title="Draft post", body="body", author=self.admin_a
        )
        self.client.login(username="admin_a", password="pw12345!")

        response = self.client.post(reverse("core:announcement_publish", args=[announcement.pk]))
        self.assertRedirects(response, reverse("core:announcements"))
        announcement.refresh_from_db()
        self.assertIsNotNone(announcement.published_at)

        response = self.client.post(
            reverse("core:announcement_unpublish", args=[announcement.pk])
        )
        self.assertRedirects(response, reverse("core:announcements"))
        announcement.refresh_from_db()
        self.assertIsNone(announcement.published_at)

    def test_edit_announcement(self):
        announcement = Announcement.objects.create(
            school=self.school_a, title="Old title", body="body", author=self.admin_a
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:announcement_edit", args=[announcement.pk]),
            {"title": "New title", "body": "updated body", "school_class": ""},
        )
        self.assertRedirects(response, reverse("core:announcements"))
        announcement.refresh_from_db()
        self.assertEqual(announcement.title, "New title")

    def test_delete_announcement(self):
        announcement = Announcement.objects.create(
            school=self.school_a, title="Gone soon", body="body", author=self.admin_a
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:announcement_delete", args=[announcement.pk]))
        self.assertRedirects(response, reverse("core:announcements"))
        self.assertFalse(Announcement.objects.filter(pk=announcement.pk).exists())

    def test_cannot_edit_other_school_announcement(self):
        announcement = Announcement.objects.create(
            school=self.school_b, title="Not yours", body="body", author=self.admin_b
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:announcement_edit", args=[announcement.pk]))
        self.assertEqual(response.status_code, 404)

    def test_staff_list_includes_drafts(self):
        Announcement.objects.create(
            school=self.school_a, title="Still a draft", body="body", author=self.admin_a
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:announcements"))
        self.assertContains(response, "Still a draft")
        self.assertTemplateUsed(response, "core/announcements_list.html")


class GuardianAnnouncementVisibilityTests(TestCase):
    """
    Covers the guardian side of Announcements: core.views.
    _guardian_announcements and announcement_mark_read. A guardian
    should only ever see published, school-wide announcements plus
    announcements for classes their own children are actually in —
    never drafts, never another class's posts.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.my_class = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.other_class = SchoolClass.objects.create(
            school=self.school_a, name="Grade 5 - Rosal", academic_year="2026-2027"
        )
        self.my_child = Student.objects.create(
            school=self.school_a, school_class=self.my_class, first_name="Ana", last_name="Reyes"
        )
        GuardianLink.objects.create(
            guardian=self.guardian, student=self.my_child, relationship=GuardianLink.Relationship.MOTHER
        )

        self.schoolwide_published = Announcement.objects.create(
            school=self.school_a,
            title="School closed Monday",
            body="body",
            author=self.admin_a,
        )
        self.schoolwide_published.published_at = self.schoolwide_published.created_at
        self.schoolwide_published.save(update_fields=["published_at"])

        self.my_class_published = Announcement.objects.create(
            school=self.school_a,
            school_class=self.my_class,
            title="Grade 4 field trip",
            body="body",
            author=self.admin_a,
        )
        self.my_class_published.published_at = self.my_class_published.created_at
        self.my_class_published.save(update_fields=["published_at"])

        self.other_class_published = Announcement.objects.create(
            school=self.school_a,
            school_class=self.other_class,
            title="Grade 5 field trip",
            body="body",
            author=self.admin_a,
        )
        self.other_class_published.published_at = self.other_class_published.created_at
        self.other_class_published.save(update_fields=["published_at"])

        self.schoolwide_draft = Announcement.objects.create(
            school=self.school_a, title="Draft — not published", body="body", author=self.admin_a
        )

    def test_guardian_sees_schoolwide_and_own_class_only(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:announcements"))
        self.assertTemplateUsed(response, "core/guardian_announcements.html")
        titles = {a.title for a in response.context["announcements"]}
        self.assertEqual(
            titles, {"School closed Monday", "Grade 4 field trip"}
        )

    def test_guardian_does_not_see_drafts(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:announcements"))
        self.assertNotContains(response, "Draft — not published")

    def test_mark_read(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:announcement_mark_read", args=[self.schoolwide_published.pk])
        )
        self.assertRedirects(response, reverse("core:announcements"))
        self.assertTrue(
            AnnouncementRead.objects.filter(
                announcement=self.schoolwide_published, guardian=self.guardian
            ).exists()
        )

    def test_cannot_mark_unrelated_class_announcement_as_read(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:announcement_mark_read", args=[self.other_class_published.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            AnnouncementRead.objects.filter(
                announcement=self.other_class_published, guardian=self.guardian
            ).exists()
        )

    def test_cannot_mark_draft_as_read(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:announcement_mark_read", args=[self.schoolwide_draft.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_my_child_detail_shows_relevant_announcements(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_detail", args=[self.my_child.pk]))
        titles = {a.title for a in response.context["announcements"]}
        self.assertEqual(titles, {"School closed Monday", "Grade 4 field trip"})


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HomeworkCRUDTests(TestCase):
    """
    Covers the admin/teacher side of Homework (Phase 1 build sequence
    item 5): core.views.homework_list/homework_new/homework_edit/
    homework_delete, and core.forms.HomeworkForm. Uses an isolated,
    temporary MEDIA_ROOT (cleaned up in tearDownClass) so attachment
    uploads during tests never touch the project's real media/ folder.
    """

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school_b, name="Grade 5 - Rosal", academic_year="2026-2027"
        )

    def test_new_homework_visible_immediately(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_new"),
            {
                "school_class": str(self.class_a.pk),
                "title": "Math worksheet",
                "description": "Pages 10-12",
                "due_date": "2026-08-01",
            },
        )
        self.assertRedirects(response, reverse("core:homework"))
        homework = Homework.objects.get(title="Math worksheet")
        self.assertEqual(homework.created_by, self.admin_a)
        self.assertEqual(homework.school_class, self.class_a)

    def test_teacher_can_create_homework(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_new"),
            {"school_class": str(self.class_a.pk), "title": "Reading log", "description": ""},
        )
        self.assertRedirects(response, reverse("core:homework"))
        self.assertTrue(Homework.objects.filter(title="Reading log").exists())

    def test_guardian_cannot_create_homework(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:homework_new"))
        self.assertEqual(response.status_code, 403)

    def test_cannot_assign_homework_to_other_school_class(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_new"),
            {"school_class": str(self.class_b.pk), "title": "Sneaky", "description": ""},
        )
        # class_b isn't in this admin's school_class queryset, so the
        # form rejects it rather than creating a cross-school record.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Homework.objects.filter(title="Sneaky").exists())

    def test_attachment_upload_and_download_link(self):
        self.client.login(username="admin_a", password="pw12345!")
        upload = SimpleUploadedFile(
            "worksheet.txt", b"homework contents", content_type="text/plain"
        )
        response = self.client.post(
            reverse("core:homework_new"),
            {
                "school_class": str(self.class_a.pk),
                "title": "With attachment",
                "description": "",
                "attachment": upload,
            },
        )
        self.assertRedirects(response, reverse("core:homework"))
        homework = Homework.objects.get(title="With attachment")
        self.assertTrue(homework.attachment.name)

    def test_edit_homework(self):
        homework = Homework.objects.create(
            school_class=self.class_a, title="Old title", created_by=self.admin_a
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_edit", args=[homework.pk]),
            {"school_class": str(self.class_a.pk), "title": "New title", "description": ""},
        )
        self.assertRedirects(response, reverse("core:homework"))
        homework.refresh_from_db()
        self.assertEqual(homework.title, "New title")

    def test_delete_homework(self):
        homework = Homework.objects.create(
            school_class=self.class_a, title="Gone soon", created_by=self.admin_a
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:homework_delete", args=[homework.pk]))
        self.assertRedirects(response, reverse("core:homework"))
        self.assertFalse(Homework.objects.filter(pk=homework.pk).exists())

    def test_cannot_edit_other_school_homework(self):
        homework = Homework.objects.create(
            school_class=self.class_b, title="Not yours", created_by=self.admin_b
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:homework_edit", args=[homework.pk]))
        self.assertEqual(response.status_code, 404)

    def test_staff_list_scoped_to_school(self):
        Homework.objects.create(
            school_class=self.class_a, title="School A homework", created_by=self.admin_a
        )
        Homework.objects.create(
            school_class=self.class_b, title="School B homework", created_by=self.admin_b
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:homework"))
        titles = {h.title for h in response.context["homework_items"]}
        self.assertEqual(titles, {"School A homework"})


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class GuardianHomeworkVisibilityTests(TestCase):
    """
    Covers the guardian side of Homework: core.views._guardian_homework.
    A guardian should only see homework for classes their own children
    are actually in — never another class's homework, even at the same
    school.
    """

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.my_class = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.other_class = SchoolClass.objects.create(
            school=self.school_a, name="Grade 5 - Rosal", academic_year="2026-2027"
        )
        self.my_child = Student.objects.create(
            school=self.school_a, school_class=self.my_class, first_name="Ana", last_name="Reyes"
        )
        GuardianLink.objects.create(
            guardian=self.guardian, student=self.my_child, relationship=GuardianLink.Relationship.MOTHER
        )

        self.my_class_homework = Homework.objects.create(
            school_class=self.my_class, title="Grade 4 worksheet", created_by=self.admin_a
        )
        self.other_class_homework = Homework.objects.create(
            school_class=self.other_class, title="Grade 5 worksheet", created_by=self.admin_a
        )

    def test_guardian_sees_only_own_class_homework(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:homework"))
        self.assertTemplateUsed(response, "core/guardian_homework.html")
        titles = {h.title for h in response.context["homework_items"]}
        self.assertEqual(titles, {"Grade 4 worksheet"})

    def test_my_child_detail_shows_relevant_homework(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_detail", args=[self.my_child.pk]))
        titles = {h.title for h in response.context["homework_items"]}
        self.assertEqual(titles, {"Grade 4 worksheet"})

    def test_guardian_with_no_children_sees_no_homework(self):
        lonely_guardian = User.objects.create_user(username="guardian_c", password="pw12345!")
        SchoolMembership.objects.create(
            user=lonely_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.client.login(username="guardian_c", password="pw12345!")
        response = self.client.get(reverse("core:homework"))
        self.assertEqual(list(response.context["homework_items"]), [])


class SchoolSetupSchoolsListTests(TestCase):
    """
    Covers the schools-list addition to core.views.school_setup: a
    superuser creating a new school can also see every school already
    in the system (not just their own), with active-only student/class
    counts, so they can check for a near-duplicate before creating one.
    Superuser-only, per the existing superuser_required on this view —
    no new scoping concern, since listing every tenant is exactly what
    a platform-operator action like this is for.
    """

    def setUp(self):
        self.superuser = User.objects.create_user(
            username="root", password="pw12345!", is_superuser=True, is_staff=True
        )
        self.regular_admin = User.objects.create_user(username="admin_a", password="pw12345!")

        self.school_a = School.objects.create(name="School A", country="PH", tier=School.Tier.FREE)
        SchoolMembership.objects.create(
            user=self.regular_admin, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.school_b = School.objects.create(name="School B", country="PH", tier=School.Tier.PAID)

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.archived_class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 3 - Ilang-Ilang", academic_year="2025-2026", is_active=False
        )
        Student.objects.create(school=self.school_a, first_name="Ana", last_name="Reyes")
        Student.objects.create(
            school=self.school_a, first_name="Ben", last_name="Santos", is_active=False
        )

    def test_superuser_sees_every_school_with_active_only_counts(self):
        self.client.login(username="root", password="pw12345!")
        response = self.client.get(reverse("core:school_setup"))
        self.assertEqual(response.status_code, 200)
        schools = {s.name: s for s in response.context["schools"]}
        self.assertIn("School A", schools)
        self.assertIn("School B", schools)
        # 2 students created for School A, only 1 is active.
        self.assertEqual(schools["School A"].active_student_count, 1)
        # 2 classes created for School A, only 1 is active.
        self.assertEqual(schools["School A"].active_class_count, 1)
        self.assertEqual(schools["School B"].active_student_count, 0)

    def test_non_superuser_admin_gets_403(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:school_setup"))
        self.assertEqual(response.status_code, 403)

    def test_creating_a_school_still_works_alongside_the_list(self):
        self.client.login(username="root", password="pw12345!")
        response = self.client.post(
            reverse("core:school_setup"),
            {
                "name": "School C",
                "country": "PH",
                "default_language": "en",
                "timezone": "Asia/Manila",
                "tier": School.Tier.FREE,
                "academic_year_start_month": 6,
            },
        )
        self.assertRedirects(response, reverse("core:dashboard"))
        self.assertTrue(School.objects.filter(name="School C").exists())


class SchoolSettingsTests(TestCase):
    """
    Covers core.views.school_settings and core.forms.SchoolSettingsForm —
    the Admin > Settings page, which edits the requesting admin's
    currently *active* school (request.school), never an id taken from
    the URL or POST data. Restricted to admin (not teacher, not
    guardian), matching the Settings sidebar link's existing
    admin-only visibility.
    """

    def setUp(self):
        self.school_a = School.objects.create(
            name="School A", country="PH", tier=School.Tier.FREE, timezone="Asia/Manila"
        )
        self.school_b = School.objects.create(name="School B", country="PH", tier=School.Tier.PAID)

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        # An admin at a second school, both memberships on one user, to
        # confirm editing always targets the *active* school.
        self.multi_school_admin = User.objects.create_user(
            username="admin_multi", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.multi_school_admin, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        SchoolMembership.objects.create(
            user=self.multi_school_admin, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

    def _settings_payload(self, **overrides):
        payload = {
            "name": "School A Renamed",
            "country": "PH",
            "default_language": "en",
            "timezone": "Asia/Manila",
            "academic_year_start_month": 6,
        }
        payload.update(overrides)
        return payload

    def test_admin_can_view_settings(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:settings"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["school"], self.school_a)

    def test_admin_can_update_settings(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:settings"), self._settings_payload())
        self.assertRedirects(response, reverse("core:settings"))
        self.school_a.refresh_from_db()
        self.assertEqual(self.school_a.name, "School A Renamed")

    def test_tier_is_not_editable_from_settings(self):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(reverse("core:settings"), self._settings_payload(tier="paid"))
        self.school_a.refresh_from_db()
        self.assertEqual(self.school_a.tier, School.Tier.FREE)

    def test_teacher_cannot_view_settings(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:settings"))
        self.assertEqual(response.status_code, 403)

    def test_guardian_cannot_view_settings(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:settings"))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("core:settings"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_edits_target_the_active_school_not_the_other_one(self):
        self.client.login(username="admin_multi", password="pw12345!")
        session = self.client.session
        session["active_school_id"] = str(self.school_b.id)
        session.save()

        response = self.client.post(
            reverse("core:settings"), self._settings_payload(name="School B Renamed")
        )
        self.assertRedirects(response, reverse("core:settings"))
        self.school_b.refresh_from_db()
        self.school_a.refresh_from_db()
        self.assertEqual(self.school_b.name, "School B Renamed")
        self.assertEqual(self.school_a.name, "School A")

    @override_settings(APP_VERSION="9.9.9-test")
    def test_settings_page_shows_app_version(self):
        # override_settings rather than depending on the real VERSION
        # file's contents, so this test doesn't need updating every
        # time the app is actually released.
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:settings"))
        self.assertEqual(response.context["app_version"], "9.9.9-test")
        self.assertContains(response, "9.9.9-test")


class FeeNoticeCRUDTests(TestCase):
    """
    Covers the admin/teacher side of Fee Notices (Phase 1 build sequence
    item 6): core.views.fee_notices_list/fee_notice_new/fee_notice_edit/
    fee_notice_delete/fee_notice_mark_paid/fee_notice_mark_waived/
    fee_notice_mark_unpaid, and core.forms.FeeNoticeForm. Unlike
    Homework, a FeeNotice is always per-student, not per-class, so the
    cross-school isolation check here posts a student from another
    school (rather than a class) at the form.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH", tier=School.Tier.PAID)
        self.school_b = School.objects.create(name="School B", country="PH", tier=School.Tier.PAID)

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.student_a = Student.objects.create(
            school=self.school_a, first_name="Ana", last_name="Reyes"
        )
        self.archived_student_a = Student.objects.create(
            school=self.school_a, first_name="Cara", last_name="Lopez", is_active=False
        )
        self.student_b = Student.objects.create(
            school=self.school_b, first_name="Ben", last_name="Torres"
        )

    def _fee_payload(self, **overrides):
        payload = {
            "student": str(self.student_a.pk),
            "title": "Term 2 tuition",
            "description": "",
            "amount": "1500.00",
            "currency": "PHP",
            "due_date": "2026-09-01",
        }
        payload.update(overrides)
        return payload

    def test_new_fee_notice_visible_immediately(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:fee_notice_new"), self._fee_payload())
        self.assertRedirects(response, reverse("core:fees"))
        fee_notice = FeeNotice.objects.get(title="Term 2 tuition")
        self.assertEqual(fee_notice.created_by, self.admin_a)
        self.assertEqual(fee_notice.school, self.school_a)
        self.assertEqual(fee_notice.student, self.student_a)
        self.assertEqual(fee_notice.status, FeeNotice.Status.UNPAID)

    def test_teacher_can_create_fee_notice(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(reverse("core:fee_notice_new"), self._fee_payload())
        self.assertRedirects(response, reverse("core:fees"))
        self.assertTrue(FeeNotice.objects.filter(title="Term 2 tuition").exists())

    def test_guardian_cannot_create_fee_notice(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:fee_notice_new"))
        self.assertEqual(response.status_code, 403)

    def test_cannot_assign_fee_notice_to_other_school_student(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:fee_notice_new"), self._fee_payload(student=str(self.student_b.pk))
        )
        # student_b isn't in this admin's student queryset, so the form
        # rejects it rather than creating a cross-school record.
        self.assertEqual(response.status_code, 200)
        self.assertFalse(FeeNotice.objects.filter(title="Term 2 tuition").exists())

    def test_archived_student_excluded_from_dropdown(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:fee_notice_new"))
        self.assertNotIn(
            self.archived_student_a, list(response.context["form"].fields["student"].queryset)
        )

    def test_edit_fee_notice(self):
        fee_notice = FeeNotice.objects.create(
            school=self.school_a,
            student=self.student_a,
            title="Old title",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_a,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:fee_notice_edit", args=[fee_notice.pk]),
            self._fee_payload(title="New title"),
        )
        self.assertRedirects(response, reverse("core:fees"))
        fee_notice.refresh_from_db()
        self.assertEqual(fee_notice.title, "New title")
        # Editing never touches status.
        self.assertEqual(fee_notice.status, FeeNotice.Status.UNPAID)

    def test_delete_fee_notice(self):
        fee_notice = FeeNotice.objects.create(
            school=self.school_a,
            student=self.student_a,
            title="Gone soon",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_a,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:fee_notice_delete", args=[fee_notice.pk]))
        self.assertRedirects(response, reverse("core:fees"))
        self.assertFalse(FeeNotice.objects.filter(pk=fee_notice.pk).exists())

    def test_cannot_edit_other_school_fee_notice(self):
        fee_notice = FeeNotice.objects.create(
            school=self.school_b,
            student=self.student_b,
            title="Not yours",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_b,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:fee_notice_edit", args=[fee_notice.pk]))
        self.assertEqual(response.status_code, 404)

    def test_mark_paid_waived_unpaid(self):
        fee_notice = FeeNotice.objects.create(
            school=self.school_a,
            student=self.student_a,
            title="Term 2 tuition",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_a,
        )
        self.client.login(username="admin_a", password="pw12345!")

        response = self.client.post(reverse("core:fee_notice_mark_paid", args=[fee_notice.pk]))
        self.assertRedirects(response, reverse("core:fees"))
        fee_notice.refresh_from_db()
        self.assertEqual(fee_notice.status, FeeNotice.Status.PAID)

        response = self.client.post(reverse("core:fee_notice_mark_waived", args=[fee_notice.pk]))
        self.assertRedirects(response, reverse("core:fees"))
        fee_notice.refresh_from_db()
        self.assertEqual(fee_notice.status, FeeNotice.Status.WAIVED)

        response = self.client.post(reverse("core:fee_notice_mark_unpaid", args=[fee_notice.pk]))
        self.assertRedirects(response, reverse("core:fees"))
        fee_notice.refresh_from_db()
        self.assertEqual(fee_notice.status, FeeNotice.Status.UNPAID)

    def test_guardian_cannot_mark_paid(self):
        fee_notice = FeeNotice.objects.create(
            school=self.school_a,
            student=self.student_a,
            title="Term 2 tuition",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_a,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(reverse("core:fee_notice_mark_paid", args=[fee_notice.pk]))
        self.assertEqual(response.status_code, 403)
        fee_notice.refresh_from_db()
        self.assertEqual(fee_notice.status, FeeNotice.Status.UNPAID)

    def test_staff_list_scoped_to_school(self):
        FeeNotice.objects.create(
            school=self.school_a,
            student=self.student_a,
            title="School A fee",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_a,
        )
        FeeNotice.objects.create(
            school=self.school_b,
            student=self.student_b,
            title="School B fee",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_b,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:fees"))
        titles = {f.title for f in response.context["fee_notices"]}
        self.assertEqual(titles, {"School A fee"})


class GuardianFeeNoticeVisibilityTests(TestCase):
    """
    Covers the guardian side of Fee Notices: core.views.
    _guardian_fee_notices, and the Fee Notices card on my_child_detail.
    A guardian should only ever see fee notices for their own linked
    children — never another family's, even at the same school.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH", tier=School.Tier.PAID)

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.other_guardian = User.objects.create_user(username="guardian_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.other_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.my_child = Student.objects.create(
            school=self.school_a, first_name="Ana", last_name="Reyes"
        )
        self.other_child = Student.objects.create(
            school=self.school_a, first_name="Ben", last_name="Santos"
        )
        GuardianLink.objects.create(
            guardian=self.guardian, student=self.my_child, relationship=GuardianLink.Relationship.MOTHER
        )
        GuardianLink.objects.create(
            guardian=self.other_guardian,
            student=self.other_child,
            relationship=GuardianLink.Relationship.MOTHER,
        )

        self.my_fee_notice = FeeNotice.objects.create(
            school=self.school_a,
            student=self.my_child,
            title="My child's fee",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_a,
        )
        self.other_fee_notice = FeeNotice.objects.create(
            school=self.school_a,
            student=self.other_child,
            title="Other child's fee",
            amount="1000.00",
            due_date="2026-09-01",
            created_by=self.admin_a,
        )

    def test_guardian_sees_only_own_child_fee_notices(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:fees"))
        self.assertTemplateUsed(response, "core/guardian_fee_notices.html")
        titles = {f.title for f in response.context["fee_notices"]}
        self.assertEqual(titles, {"My child's fee"})

    def test_my_child_detail_shows_relevant_fee_notices(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_detail", args=[self.my_child.pk]))
        titles = {f.title for f in response.context["fee_notices"]}
        self.assertEqual(titles, {"My child's fee"})

    def test_guardian_with_no_children_sees_no_fee_notices(self):
        lonely_guardian = User.objects.create_user(username="guardian_c", password="pw12345!")
        SchoolMembership.objects.create(
            user=lonely_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.client.login(username="guardian_c", password="pw12345!")
        response = self.client.get(reverse("core:fees"))
        self.assertEqual(list(response.context["fee_notices"]), [])


class PermissionSlipCRUDTests(TestCase):
    """
    Covers the admin/teacher side of Permission Slips (Phase 1 build
    sequence item 7): core.views.permission_slips_list/permission_slip_new/
    permission_slip_detail/permission_slip_edit/permission_slip_delete,
    core.forms.PermissionSlipForm, and the response-row auto-generation
    in core.views._sync_permission_slip_responses.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.other_class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 5 - Rosal", academic_year="2026-2027"
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school_b, name="Grade 5 - Rosal", academic_year="2026-2027"
        )

        self.student_with_guardian = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        GuardianLink.objects.create(
            guardian=self.guardian_a,
            student=self.student_with_guardian,
            relationship=GuardianLink.Relationship.MOTHER,
            is_primary_contact=True,
        )
        self.student_no_guardian = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ben", last_name="Santos"
        )
        self.student_archived = Student.objects.create(
            school=self.school_a,
            school_class=self.class_a,
            first_name="Cara",
            last_name="Lopez",
            is_active=False,
        )
        self.student_other_class = Student.objects.create(
            school=self.school_a, school_class=self.other_class_a, first_name="Deo", last_name="Cruz"
        )

    def _slip_payload(self, **overrides):
        payload = {
            "title": "Museum field trip",
            "description": "",
            "school_class": str(self.class_a.pk),
            "event_date": "2026-09-15",
            "response_deadline": "2026-09-08",
        }
        payload.update(overrides)
        return payload

    def test_new_class_scoped_slip_generates_responses_for_eligible_students_only(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:permission_slip_new"), self._slip_payload())
        self.assertRedirects(response, reverse("core:permission_slips"))

        slip = PermissionSlip.objects.get(title="Museum field trip")
        self.assertEqual(slip.school, self.school_a)
        self.assertEqual(slip.created_by, self.admin_a)

        response_students = set(
            PermissionSlipResponse.objects.filter(permission_slip=slip).values_list(
                "student_id", flat=True
            )
        )
        self.assertEqual(response_students, {self.student_with_guardian.pk})
        pr = PermissionSlipResponse.objects.get(permission_slip=slip, student=self.student_with_guardian)
        self.assertEqual(pr.response, PermissionSlipResponse.Response.PENDING)
        self.assertEqual(pr.guardian, self.guardian_a)

    def test_new_school_wide_slip_covers_all_classes(self):
        GuardianLink.objects.create(
            guardian=self.guardian_a,
            student=self.student_other_class,
            relationship=GuardianLink.Relationship.FATHER,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:permission_slip_new"), self._slip_payload(school_class="")
        )
        self.assertRedirects(response, reverse("core:permission_slips"))
        slip = PermissionSlip.objects.get(title="Museum field trip")
        self.assertIsNone(slip.school_class)
        response_students = set(
            PermissionSlipResponse.objects.filter(permission_slip=slip).values_list(
                "student_id", flat=True
            )
        )
        self.assertEqual(
            response_students, {self.student_with_guardian.pk, self.student_other_class.pk}
        )

    def test_teacher_can_create_permission_slip(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(reverse("core:permission_slip_new"), self._slip_payload())
        self.assertRedirects(response, reverse("core:permission_slips"))
        self.assertTrue(PermissionSlip.objects.filter(title="Museum field trip").exists())

    def test_guardian_cannot_create_permission_slip(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:permission_slip_new"))
        self.assertEqual(response.status_code, 403)

    def test_cannot_assign_to_other_school_class(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:permission_slip_new"),
            self._slip_payload(school_class=str(self.class_b.pk)),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PermissionSlip.objects.filter(title="Museum field trip").exists())

    def test_edit_widening_audience_adds_new_responses_without_touching_existing(self):
        slip = PermissionSlip.objects.create(
            school=self.school_a, school_class=self.class_a, title="Sports Day", created_by=self.admin_a
        )
        _sync_permission_slip_responses(slip)
        existing_response = PermissionSlipResponse.objects.get(
            permission_slip=slip, student=self.student_with_guardian
        )
        existing_response.response = PermissionSlipResponse.Response.YES
        existing_response.save(update_fields=["response"])

        GuardianLink.objects.create(
            guardian=self.guardian_a,
            student=self.student_other_class,
            relationship=GuardianLink.Relationship.FATHER,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:permission_slip_edit", args=[slip.pk]),
            self._slip_payload(title="Sports Day", school_class=""),
        )
        self.assertRedirects(response, reverse("core:permission_slips"))
        slip.refresh_from_db()
        self.assertIsNone(slip.school_class)

        existing_response.refresh_from_db()
        self.assertEqual(existing_response.response, PermissionSlipResponse.Response.YES)

        self.assertTrue(
            PermissionSlipResponse.objects.filter(
                permission_slip=slip, student=self.student_other_class
            ).exists()
        )

    def test_delete_permission_slip_cascades_responses(self):
        slip = PermissionSlip.objects.create(
            school=self.school_a, school_class=self.class_a, title="Gone soon", created_by=self.admin_a
        )
        _sync_permission_slip_responses(slip)
        self.assertTrue(PermissionSlipResponse.objects.filter(permission_slip=slip).exists())

        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(reverse("core:permission_slip_delete", args=[slip.pk]))
        self.assertRedirects(response, reverse("core:permission_slips"))
        self.assertFalse(PermissionSlip.objects.filter(pk=slip.pk).exists())
        self.assertFalse(PermissionSlipResponse.objects.filter(permission_slip_id=slip.pk).exists())

    def test_cannot_view_other_school_permission_slip(self):
        slip = PermissionSlip.objects.create(
            school=self.school_b, title="Not yours", created_by=self.admin_b
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:permission_slip_detail", args=[slip.pk]))
        self.assertEqual(response.status_code, 404)

    def test_list_shows_response_counts(self):
        slip = PermissionSlip.objects.create(
            school=self.school_a, school_class=self.class_a, title="Museum field trip", created_by=self.admin_a
        )
        _sync_permission_slip_responses(slip)
        pr = PermissionSlipResponse.objects.get(permission_slip=slip, student=self.student_with_guardian)
        pr.response = PermissionSlipResponse.Response.YES
        pr.save(update_fields=["response"])

        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:permission_slips"))
        slips = {s.pk: s for s in response.context["permission_slips"]}
        self.assertEqual(slips[slip.pk].response_yes, 1)
        self.assertEqual(slips[slip.pk].response_no, 0)
        self.assertEqual(slips[slip.pk].response_pending, 0)


class GuardianPermissionSlipResponseTests(TestCase):
    """
    Covers the guardian side of Permission Slips: core.views.
    _guardian_permission_slips and permission_slip_respond. A guardian
    should only see and respond to permission slips for their own
    linked children, and responding should be scoped fresh via
    GuardianLink rather than trusting the pre-seeded response row.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.other_guardian = User.objects.create_user(username="guardian_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.other_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.my_class = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.other_class = SchoolClass.objects.create(
            school=self.school_a, name="Grade 5 - Rosal", academic_year="2026-2027"
        )
        self.my_child = Student.objects.create(
            school=self.school_a, school_class=self.my_class, first_name="Ana", last_name="Reyes"
        )
        self.other_child = Student.objects.create(
            school=self.school_a, school_class=self.other_class, first_name="Ben", last_name="Santos"
        )
        GuardianLink.objects.create(
            guardian=self.guardian, student=self.my_child, relationship=GuardianLink.Relationship.MOTHER
        )
        GuardianLink.objects.create(
            guardian=self.other_guardian,
            student=self.other_child,
            relationship=GuardianLink.Relationship.MOTHER,
        )

        self.my_class_slip = PermissionSlip.objects.create(
            school=self.school_a, school_class=self.my_class, title="Grade 4 trip", created_by=self.admin_a
        )
        self.other_class_slip = PermissionSlip.objects.create(
            school=self.school_a, school_class=self.other_class, title="Grade 5 trip", created_by=self.admin_a
        )
        self.schoolwide_slip = PermissionSlip.objects.create(
            school=self.school_a, title="School-wide photo consent", created_by=self.admin_a
        )

    def test_guardian_sees_only_own_children_response_rows(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:permission_slips"))
        self.assertTemplateUsed(response, "core/guardian_permission_slips.html")
        titles = {
            item["permission_slip"].title for item in response.context["response_items"]
        }
        self.assertEqual(titles, {"Grade 4 trip", "School-wide photo consent"})
        students = {
            item["response"].student for item in response.context["response_items"]
        }
        self.assertEqual(students, {self.my_child})

    def test_respond_yes(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse(
                "core:permission_slip_respond", args=[self.my_class_slip.pk, self.my_child.pk]
            ),
            {"response": "yes", "notes": "Approved, thanks!"},
        )
        self.assertRedirects(response, reverse("core:permission_slips"))
        pr = PermissionSlipResponse.objects.get(
            permission_slip=self.my_class_slip, student=self.my_child
        )
        self.assertEqual(pr.response, PermissionSlipResponse.Response.YES)
        self.assertEqual(pr.guardian, self.guardian)
        self.assertEqual(pr.notes, "Approved, thanks!")
        self.assertIsNotNone(pr.responded_at)

    def test_respond_no(self):
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.post(
            reverse(
                "core:permission_slip_respond", args=[self.my_class_slip.pk, self.my_child.pk]
            ),
            {"response": "no", "notes": ""},
        )
        pr = PermissionSlipResponse.objects.get(
            permission_slip=self.my_class_slip, student=self.my_child
        )
        self.assertEqual(pr.response, PermissionSlipResponse.Response.NO)

    def test_can_change_response(self):
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.post(
            reverse(
                "core:permission_slip_respond", args=[self.my_class_slip.pk, self.my_child.pk]
            ),
            {"response": "yes", "notes": ""},
        )
        self.client.post(
            reverse(
                "core:permission_slip_respond", args=[self.my_class_slip.pk, self.my_child.pk]
            ),
            {"response": "no", "notes": "Changed my mind"},
        )
        pr = PermissionSlipResponse.objects.get(
            permission_slip=self.my_class_slip, student=self.my_child
        )
        self.assertEqual(pr.response, PermissionSlipResponse.Response.NO)
        self.assertEqual(pr.notes, "Changed my mind")

    def test_cannot_respond_for_unrelated_child(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse(
                "core:permission_slip_respond",
                args=[self.other_class_slip.pk, self.other_child.pk],
            ),
            {"response": "yes", "notes": ""},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            PermissionSlipResponse.objects.filter(
                permission_slip=self.other_class_slip, student=self.other_child, guardian=self.guardian
            ).exists()
        )

    def test_invalid_response_value_rejected(self):
        # Note: deliberately checks status_code/url directly rather than
        # assertRedirects, which follows the redirect by default — that
        # follow-up GET to the permission slips list would itself
        # self-heal a pending response row into existence (correct
        # behavior for viewing the list) and defeat the point of this
        # test, which is confirming the invalid POST itself created
        # nothing.
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse(
                "core:permission_slip_respond", args=[self.my_class_slip.pk, self.my_child.pk]
            ),
            {"response": "maybe", "notes": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:permission_slips"))
        self.assertFalse(
            PermissionSlipResponse.objects.filter(
                permission_slip=self.my_class_slip, student=self.my_child
            ).exists()
        )

    def test_response_row_self_heals_for_guardian_linked_after_slip_created(self):
        # A slip created before this guardian-student link existed still
        # gets a response row the first time the guardian views the list.
        late_child = Student.objects.create(
            school=self.school_a, school_class=self.my_class, first_name="Cara", last_name="Lopez"
        )
        GuardianLink.objects.create(
            guardian=self.guardian, student=late_child, relationship=GuardianLink.Relationship.MOTHER
        )
        self.assertFalse(
            PermissionSlipResponse.objects.filter(
                permission_slip=self.my_class_slip, student=late_child
            ).exists()
        )
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.get(reverse("core:permission_slips"))
        self.assertTrue(
            PermissionSlipResponse.objects.filter(
                permission_slip=self.my_class_slip, student=late_child
            ).exists()
        )

    def test_my_child_detail_shows_relevant_permission_slip_responses(self):
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.get(reverse("core:permission_slips"))  # trigger sync
        response = self.client.get(reverse("core:my_child_detail", args=[self.my_child.pk]))
        titles = {
            r.permission_slip.title for r in response.context["permission_slip_responses"]
        }
        self.assertEqual(titles, {"Grade 4 trip", "School-wide photo consent"})


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HomeworkSubmissionRosterTests(TestCase):
    """
    Covers the teacher/admin side of homework submissions:
    core.views.homework_detail's per-student roster and the
    accepts_submissions toggle on core.forms.HomeworkForm.
    submitted/late/missing/not-yet-submitted is derived purely from
    due_date vs. whether/when a HomeworkSubmission row exists — none of
    it is a manually-set value, so these tests seed rows directly onto
    the model rather than only exercising it through homework_submit.
    """

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school_b, name="Grade 5 - Rosal", academic_year="2026-2027"
        )

        self.student_submitted = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        self.student_late = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ben", last_name="Santos"
        )
        self.student_missing = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Cara", last_name="Lopez"
        )
        GuardianLink.objects.create(guardian=self.guardian_a, student=self.student_submitted)
        GuardianLink.objects.create(guardian=self.guardian_a, student=self.student_late)
        GuardianLink.objects.create(guardian=self.guardian_a, student=self.student_missing)

        self.homework = Homework.objects.create(
            school_class=self.class_a,
            title="Reading log",
            due_date=date(2026, 7, 20),
            accepts_submissions=True,
            created_by=self.admin_a,
        )
        HomeworkSubmission.objects.create(
            homework=self.homework,
            student=self.student_submitted,
            file=SimpleUploadedFile("page.jpg", b"img-bytes", content_type="image/jpeg"),
            submitted_at=timezone.make_aware(datetime(2026, 7, 18, 9, 0)),
            status=HomeworkSubmission.Status.SUBMITTED,
        )
        HomeworkSubmission.objects.create(
            homework=self.homework,
            student=self.student_late,
            file=SimpleUploadedFile("page2.jpg", b"img-bytes", content_type="image/jpeg"),
            submitted_at=timezone.make_aware(datetime(2026, 7, 22, 9, 0)),
            status=HomeworkSubmission.Status.LATE,
        )
        # student_missing has no HomeworkSubmission row at all — that
        # absence is what "missing" means, not a stored status value.

    def test_roster_shows_submitted_late_and_missing(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:homework_detail", args=[self.homework.pk]))
        statuses = {row["student"].id: row["status"] for row in response.context["roster"]}
        self.assertEqual(statuses[self.student_submitted.id], "submitted")
        self.assertEqual(statuses[self.student_late.id], "late")
        self.assertEqual(statuses[self.student_missing.id], "missing")

    def test_not_yet_due_shows_not_submitted_rather_than_missing(self):
        self.homework.due_date = date(2099, 1, 1)
        self.homework.save(update_fields=["due_date"])
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:homework_detail", args=[self.homework.pk]))
        statuses = {row["student"].id: row["status"] for row in response.context["roster"]}
        self.assertEqual(statuses[self.student_missing.id], "not_submitted")

    def test_cannot_view_other_school_homework_roster(self):
        other_homework = Homework.objects.create(
            school_class=self.class_b, title="Not yours", created_by=self.admin_b
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:homework_detail", args=[other_homework.pk]))
        self.assertEqual(response.status_code, 404)

    def test_guardian_cannot_view_roster(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:homework_detail", args=[self.homework.pk]))
        self.assertEqual(response.status_code, 403)

    def test_accepts_submissions_defaults_off(self):
        homework = Homework.objects.create(
            school_class=self.class_a, title="Plain homework", created_by=self.admin_a
        )
        self.assertFalse(homework.accepts_submissions)

    def test_accepts_submissions_can_be_turned_on_via_form(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_new"),
            {
                "school_class": str(self.class_a.pk),
                "title": "New homework",
                "description": "",
                "accepts_submissions": "on",
            },
        )
        self.assertRedirects(response, reverse("core:homework"))
        homework = Homework.objects.get(title="New homework")
        self.assertTrue(homework.accepts_submissions)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HomeworkSubmissionGuardianPermissionTests(TestCase):
    """
    Covers the guardian side of homework submissions and the permission
    boundary the development plan calls out explicitly: a guardian
    cannot see or submit for a student they aren't linked to, even
    within the same school and class, and can't submit at all against
    homework that doesn't accept submissions.
    """

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.other_guardian = User.objects.create_user(username="guardian_z", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.other_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4 - Sampaguita", academic_year="2026-2027"
        )
        self.my_child = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        self.unrelated_child = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ben", last_name="Cruz"
        )
        GuardianLink.objects.create(guardian=self.guardian, student=self.my_child)
        GuardianLink.objects.create(guardian=self.other_guardian, student=self.unrelated_child)

        self.homework = Homework.objects.create(
            school_class=self.class_a,
            title="Reading log",
            due_date=date(2026, 8, 15),
            accepts_submissions=True,
            created_by=self.admin_a,
        )
        self.closed_homework = Homework.objects.create(
            school_class=self.class_a,
            title="No submissions here",
            accepts_submissions=False,
            created_by=self.admin_a,
        )

    def _upload(self, name="page.jpg", content=b"img-bytes", content_type="image/jpeg"):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def test_guardian_can_submit_for_own_child(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {"file": self._upload(), "note": "Done!"},
        )
        self.assertRedirects(response, reverse("core:homework"))
        submission = HomeworkSubmission.objects.get(homework=self.homework, student=self.my_child)
        self.assertEqual(submission.status, HomeworkSubmission.Status.SUBMITTED)
        self.assertEqual(submission.submitted_by, self.guardian)
        self.assertEqual(submission.note, "Done!")

    def test_guardian_cannot_submit_for_unlinked_student(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.unrelated_child.pk]),
            {"file": self._upload(), "note": ""},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            HomeworkSubmission.objects.filter(
                homework=self.homework, student=self.unrelated_child
            ).exists()
        )

    def test_guardian_cannot_submit_when_homework_does_not_accept_submissions(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.closed_homework.pk, self.my_child.pk]),
            {"file": self._upload(), "note": ""},
        )
        self.assertEqual(response.status_code, 404)

    def test_other_guardian_cannot_submit_for_someone_elses_child(self):
        # guardian_z is a real guardian account at this school, just not
        # linked to my_child — a valid guardian login isn't enough, the
        # link has to be to *this* student.
        self.client.login(username="guardian_z", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {"file": self._upload(), "note": ""},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            HomeworkSubmission.objects.filter(homework=self.homework, student=self.my_child).exists()
        )

    def test_guardian_list_only_shows_own_childs_submission(self):
        HomeworkSubmission.objects.create(
            homework=self.homework,
            student=self.unrelated_child,
            file=self._upload("other.jpg"),
            submitted_at=timezone.now(),
            status=HomeworkSubmission.Status.SUBMITTED,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:homework"))
        homework_items = list(response.context["homework_items"])
        rows = next(h for h in homework_items if h.pk == self.homework.pk).my_submission_rows
        student_ids = {row["student"].id for row in rows}
        self.assertEqual(student_ids, {self.my_child.id})

    def test_replace_before_due_date(self):
        HomeworkSubmission.objects.create(
            homework=self.homework,
            student=self.my_child,
            file=self._upload("first.jpg"),
            submitted_at=timezone.now(),
            status=HomeworkSubmission.Status.SUBMITTED,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {"file": self._upload("second.jpg"), "note": "Better version"},
        )
        self.assertRedirects(response, reverse("core:homework"))
        submission = HomeworkSubmission.objects.get(homework=self.homework, student=self.my_child)
        self.assertEqual(submission.note, "Better version")
        self.assertIn("second", submission.file.name)

    def test_cannot_replace_after_due_date_passed(self):
        HomeworkSubmission.objects.create(
            homework=self.homework,
            student=self.my_child,
            file=self._upload("first.jpg"),
            submitted_at=timezone.now(),
            status=HomeworkSubmission.Status.SUBMITTED,
        )
        self.homework.due_date = date(2020, 1, 1)
        self.homework.save(update_fields=["due_date"])
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {"file": self._upload("second.jpg"), "note": ""},
        )
        self.assertRedirects(response, reverse("core:homework"))
        submission = HomeworkSubmission.objects.get(homework=self.homework, student=self.my_child)
        # Still the original file — the replace attempt was rejected.
        self.assertIn("first", submission.file.name)

    def test_first_submission_after_due_date_is_accepted_and_marked_late(self):
        self.homework.due_date = date(2020, 1, 1)
        self.homework.save(update_fields=["due_date"])
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {"file": self._upload(), "note": ""},
        )
        self.assertRedirects(response, reverse("core:homework"))
        submission = HomeworkSubmission.objects.get(homework=self.homework, student=self.my_child)
        self.assertEqual(submission.status, HomeworkSubmission.Status.LATE)

    def test_any_linked_guardian_can_see_and_replace_current_state(self):
        # Multi-guardian household: a second guardian linked to the same
        # child sees the first guardian's submission and can replace it.
        second_guardian = User.objects.create_user(username="guardian_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=second_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        GuardianLink.objects.create(guardian=second_guardian, student=self.my_child)

        HomeworkSubmission.objects.create(
            homework=self.homework,
            student=self.my_child,
            file=self._upload("mom.jpg"),
            submitted_at=timezone.now(),
            status=HomeworkSubmission.Status.SUBMITTED,
            submitted_by=self.guardian,
        )

        self.client.login(username="guardian_b", password="pw12345!")
        list_response = self.client.get(reverse("core:homework"))
        rows = next(
            h for h in list_response.context["homework_items"] if h.pk == self.homework.pk
        ).my_submission_rows
        self.assertEqual(rows[0]["status"], "submitted")

        submit_response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {"file": self._upload("dad.jpg"), "note": ""},
        )
        self.assertRedirects(submit_response, reverse("core:homework"))
        submission = HomeworkSubmission.objects.get(homework=self.homework, student=self.my_child)
        self.assertEqual(submission.submitted_by, second_guardian)
        self.assertIn("dad", submission.file.name)

    def test_rejects_disallowed_file_extension(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {
                "file": SimpleUploadedFile(
                    "virus.exe", b"nope", content_type="application/octet-stream"
                ),
                "note": "",
            },
        )
        self.assertRedirects(response, reverse("core:homework"))
        self.assertFalse(
            HomeworkSubmission.objects.filter(homework=self.homework, student=self.my_child).exists()
        )

    def test_rejects_oversized_file(self):
        self.client.login(username="guardian_a", password="pw12345!")
        big_content = b"a" * (10 * 1024 * 1024 + 1)
        response = self.client.post(
            reverse("core:homework_submit", args=[self.homework.pk, self.my_child.pk]),
            {"file": self._upload("big.jpg", big_content), "note": ""},
        )
        self.assertRedirects(response, reverse("core:homework"))
        self.assertFalse(
            HomeworkSubmission.objects.filter(homework=self.homework, student=self.my_child).exists()
        )


class SchoolCalendarEventTests(TestCase):
    """
    Covers the School Calendar feature: core.models.SchoolCalendarEvent,
    core.forms.SchoolCalendarEventForm, and core.views.calendar_list/
    calendar_new/calendar_edit/calendar_delete/calendar_closed_days_json.
    Write access is deliberately admin-only here (not admin+teacher, the
    shape most other sections in this app use) — teachers get the same
    read-only view guardians get, per the feature's "an admin maintains
    a list" design.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.today = timezone.localdate()

    def test_model_rejects_end_before_start(self):
        event = SchoolCalendarEvent(
            school=self.school_a,
            label="Bad range",
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 1),
        )
        with self.assertRaises(ValidationError):
            event.full_clean()

    def test_admin_can_create_closed_day(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:calendar_new"),
            {
                "label": "Independence Day",
                "start_date": "2026-08-15",
                "end_date": "2026-08-15",
                "event_type": "holiday",
            },
        )
        self.assertRedirects(response, reverse("core:calendar"))
        event = SchoolCalendarEvent.objects.get(label="Independence Day")
        self.assertEqual(event.school, self.school_a)
        self.assertEqual(event.created_by, self.admin_a)

    def test_teacher_cannot_create_closed_day(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar_new"))
        self.assertEqual(response.status_code, 403)

    def test_guardian_cannot_create_closed_day(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar_new"))
        self.assertEqual(response.status_code, 403)

    def test_form_rejects_end_before_start(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:calendar_new"),
            {
                "label": "Bad range",
                "start_date": "2026-08-15",
                "end_date": "2026-08-01",
                "event_type": "other",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SchoolCalendarEvent.objects.filter(label="Bad range").exists())

    def test_admin_can_edit_and_delete(self):
        event = SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="Old label",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            created_by=self.admin_a,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:calendar_edit", args=[event.pk]),
            {
                "label": "New label",
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
                "event_type": "in_service",
            },
        )
        self.assertRedirects(response, reverse("core:calendar"))
        event.refresh_from_db()
        self.assertEqual(event.label, "New label")
        self.assertEqual(event.event_type, "in_service")

        response = self.client.post(reverse("core:calendar_delete", args=[event.pk]))
        self.assertRedirects(response, reverse("core:calendar"))
        self.assertFalse(SchoolCalendarEvent.objects.filter(pk=event.pk).exists())

    def test_teacher_cannot_edit_or_delete(self):
        event = SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="Protected",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            created_by=self.admin_a,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar_edit", args=[event.pk]))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse("core:calendar_delete", args=[event.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(SchoolCalendarEvent.objects.filter(pk=event.pk).exists())

    def test_admin_cannot_edit_other_schools_event(self):
        event = SchoolCalendarEvent.objects.create(
            school=self.school_b,
            label="Not yours",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            created_by=self.admin_b,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar_edit", args=[event.pk]))
        self.assertEqual(response.status_code, 404)

    def test_admin_sees_past_and_future_events(self):
        SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="Past break",
            start_date=self.today - timedelta(days=30),
            end_date=self.today - timedelta(days=25),
            created_by=self.admin_a,
        )
        SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="Upcoming holiday",
            start_date=self.today + timedelta(days=10),
            end_date=self.today + timedelta(days=10),
            created_by=self.admin_a,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar"))
        self.assertTemplateUsed(response, "core/calendar_list.html")
        labels = {e.label for e in response.context["events"]}
        self.assertEqual(labels, {"Past break", "Upcoming holiday"})

    def test_teacher_and_guardian_see_only_upcoming(self):
        SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="Past break",
            start_date=self.today - timedelta(days=30),
            end_date=self.today - timedelta(days=25),
            created_by=self.admin_a,
        )
        SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="Upcoming holiday",
            start_date=self.today + timedelta(days=10),
            end_date=self.today + timedelta(days=10),
            created_by=self.admin_a,
        )
        for username in ("teacher_a", "guardian_a"):
            self.client.login(username=username, password="pw12345!")
            response = self.client.get(reverse("core:calendar"))
            self.assertTemplateUsed(response, "core/calendar_readonly.html")
            labels = {e.label for e in response.context["events"]}
            self.assertEqual(labels, {"Upcoming holiday"})
            self.client.logout()

    def test_closed_days_json_scoped_to_school(self):
        SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="School A holiday",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 15),
            event_type="holiday",
            created_by=self.admin_a,
        )
        SchoolCalendarEvent.objects.create(
            school=self.school_b,
            label="School B holiday",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 15),
            event_type="holiday",
            created_by=self.admin_b,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar_closed_days_json"))
        self.assertEqual(response.status_code, 200)
        labels = {d["label"] for d in response.json()["closed_days"]}
        self.assertEqual(labels, {"School A holiday"})

    def test_guardian_can_fetch_closed_days_json(self):
        SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="Holiday",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 15),
            created_by=self.admin_a,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar_closed_days_json"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["closed_days"]), 1)

    def test_closed_days_json_date_range_does_not_leak_across_years(self):
        SchoolCalendarEvent.objects.create(
            school=self.school_a,
            label="2026 break",
            start_date=date(2026, 12, 20),
            end_date=date(2027, 1, 5),
            created_by=self.admin_a,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:calendar_closed_days_json"))
        data = response.json()["closed_days"][0]
        # A date-range check for a year before this event correctly
        # falls outside [start, end] — no recurring-event logic means
        # last year's dates are never accidentally covered.
        self.assertFalse(data["start"] <= "2025-12-22" <= data["end"])
        self.assertTrue(data["start"] <= "2026-12-22" <= data["end"])

    def test_homework_form_due_date_has_warning_attribute(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:homework_new"))
        self.assertContains(response, "data-warn-closed-days")
        # Not "calendar-warnings.js" verbatim — WhiteNoise's manifest
        # storage inserts a content hash before the extension in
        # production-like test runs (e.g. calendar-warnings.abcd1234.js).
        self.assertContains(response, "calendar-warnings")


class AttendanceTests(TestCase):
    """
    Covers the Attendance Tracking feature: core.models.AttendanceRecord,
    and core.views.attendance_classes_list/attendance_roster/
    attendance_overview/my_child_attendance. The permission boundary
    that matters most here (per the roadmap: "visible to a student's
    own guardians — nobody else's") is that a guardian can never reach
    another family's child's record, even indirectly through a
    class-level view — that's exercised explicitly below, not just
    "guardian sees their own child's data" in isolation.
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.other_guardian_a = User.objects.create_user(
            username="other_guardian_a", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.other_guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4", academic_year="2026-2027"
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school_b, name="Grade 4", academic_year="2026-2027"
        )

        self.student = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        self.other_student = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ben", last_name="Cruz"
        )
        GuardianLink.objects.create(guardian=self.guardian_a, student=self.student)
        GuardianLink.objects.create(guardian=self.other_guardian_a, student=self.other_student)

        self.today = timezone.localdate()

    def test_model_unique_constraint_per_student_per_day(self):
        AttendanceRecord.objects.create(
            student=self.student, school_class=self.class_a, date=self.today,
            status=AttendanceRecord.Status.PRESENT,
        )
        with self.assertRaises(Exception):
            AttendanceRecord.objects.create(
                student=self.student, school_class=self.class_a, date=self.today,
                status=AttendanceRecord.Status.ABSENT,
            )

    def test_teacher_can_take_todays_roster(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:attendance_roster", args=[self.class_a.pk]),
            {
                f"status-{self.student.pk}": "absent",
                f"status-{self.other_student.pk}": "present",
            },
        )
        self.assertRedirects(
            response,
            f"{reverse('core:attendance_roster', args=[self.class_a.pk])}?date={self.today.isoformat()}",
        )
        record = AttendanceRecord.objects.get(student=self.student, date=self.today)
        self.assertEqual(record.status, AttendanceRecord.Status.ABSENT)
        self.assertEqual(record.recorded_by, self.teacher_a)
        self.assertEqual(
            AttendanceRecord.objects.get(student=self.other_student, date=self.today).status,
            AttendanceRecord.Status.PRESENT,
        )

    def test_retaking_todays_roster_updates_in_place(self):
        self.client.login(username="teacher_a", password="pw12345!")
        self.client.post(
            reverse("core:attendance_roster", args=[self.class_a.pk]),
            {f"status-{self.student.pk}": "absent", f"status-{self.other_student.pk}": "present"},
        )
        self.client.post(
            reverse("core:attendance_roster", args=[self.class_a.pk]),
            {f"status-{self.student.pk}": "late", f"status-{self.other_student.pk}": "present"},
        )
        self.assertEqual(AttendanceRecord.objects.filter(student=self.student).count(), 1)
        self.assertEqual(
            AttendanceRecord.objects.get(student=self.student).status, AttendanceRecord.Status.LATE
        )

    def test_teacher_cannot_edit_older_entry(self):
        old_date = self.today - timedelta(days=5)
        AttendanceRecord.objects.create(
            student=self.student, school_class=self.class_a, date=old_date,
            status=AttendanceRecord.Status.PRESENT, recorded_by=self.teacher_a,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            f"{reverse('core:attendance_roster', args=[self.class_a.pk])}?date={old_date.isoformat()}",
            {f"status-{self.student.pk}": "absent"},
        )
        self.assertEqual(response.status_code, 403)
        record = AttendanceRecord.objects.get(student=self.student, date=old_date)
        self.assertEqual(record.status, AttendanceRecord.Status.PRESENT)

    def test_admin_can_amend_older_entry(self):
        old_date = self.today - timedelta(days=5)
        AttendanceRecord.objects.create(
            student=self.student, school_class=self.class_a, date=old_date,
            status=AttendanceRecord.Status.PRESENT, recorded_by=self.teacher_a,
        )
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            f"{reverse('core:attendance_roster', args=[self.class_a.pk])}?date={old_date.isoformat()}",
            {f"status-{self.student.pk}": "absent"},
        )
        self.assertRedirects(
            response,
            f"{reverse('core:attendance_roster', args=[self.class_a.pk])}?date={old_date.isoformat()}",
        )
        record = AttendanceRecord.objects.get(student=self.student, date=old_date)
        self.assertEqual(record.status, AttendanceRecord.Status.ABSENT)
        self.assertEqual(record.recorded_by, self.admin_a)

    def test_cannot_take_attendance_for_a_future_date(self):
        self.client.login(username="teacher_a", password="pw12345!")
        future = self.today + timedelta(days=1)
        response = self.client.get(
            f"{reverse('core:attendance_roster', args=[self.class_a.pk])}?date={future.isoformat()}"
        )
        self.assertRedirects(
            response, reverse("core:attendance_roster", args=[self.class_a.pk])
        )
        self.assertFalse(AttendanceRecord.objects.filter(date=future).exists())

    def test_guardian_cannot_reach_class_roster(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:attendance_roster", args=[self.class_a.pk]))
        self.assertEqual(response.status_code, 403)

    def test_guardian_cannot_reach_classes_list_or_overview(self):
        self.client.login(username="guardian_a", password="pw12345!")
        self.assertEqual(self.client.get(reverse("core:attendance_classes")).status_code, 403)
        self.assertEqual(self.client.get(reverse("core:attendance_overview")).status_code, 403)

    def test_teacher_cannot_reach_admin_overview(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:attendance_overview"))
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_reach_other_schools_class_roster(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:attendance_roster", args=[self.class_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_guardian_sees_only_own_childs_attendance(self):
        AttendanceRecord.objects.create(
            student=self.student, school_class=self.class_a, date=self.today,
            status=AttendanceRecord.Status.ABSENT, recorded_by=self.teacher_a,
        )
        AttendanceRecord.objects.create(
            student=self.other_student, school_class=self.class_a, date=self.today,
            status=AttendanceRecord.Status.LATE, recorded_by=self.teacher_a,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_attendance", args=[self.student.pk]))
        self.assertEqual(response.status_code, 200)
        records = response.context["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].student, self.student)

    def test_guardian_cannot_view_another_familys_child_attendance(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(
            reverse("core:my_child_attendance", args=[self.other_student.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_class_detail_shows_not_yet_taken_indicator(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:class_detail", args=[self.class_a.pk]))
        self.assertFalse(response.context["attendance_taken_today"])
        self.assertContains(response, "Not taken today")

        AttendanceRecord.objects.create(
            student=self.student, school_class=self.class_a, date=self.today,
            status=AttendanceRecord.Status.PRESENT, recorded_by=self.teacher_a,
        )
        response = self.client.get(reverse("core:class_detail", args=[self.class_a.pk]))
        self.assertTrue(response.context["attendance_taken_today"])


class ReportCardTests(TestCase):
    """
    Covers the Report Cards per Student feature: core.models.Term/
    ReportCard/ReportCardEntry/ReportCardRead, and core.views.term_*/
    report_cards_classes_list/report_cards_roster/
    report_cards_publish_all/report_card_edit/report_card_view/
    my_child_report_cards. The two permission boundaries the roadmap
    calls out explicitly are exercised directly: "a teacher can only
    enter reports for students in their own class" and "a guardian can
    only see their own linked students' [published] reports" — plus the
    multi-guardian-household case (both guardians see the same
    published report).
    """

    def setUp(self):
        self.school_a = School.objects.create(name="School A", country="PH")
        self.school_b = School.objects.create(name="School B", country="PH")

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_a = User.objects.create_user(username="teacher_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        # A second teacher at the same school who does *not* teach class_a
        # — the one who should be blocked from entering its reports.
        self.other_teacher_a = User.objects.create_user(
            username="other_teacher_a", password="pw12345!"
        )
        self.other_teacher_membership = SchoolMembership.objects.create(
            user=self.other_teacher_a, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.second_guardian_a = User.objects.create_user(
            username="second_guardian_a", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.second_guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.other_guardian_a = User.objects.create_user(
            username="other_guardian_a", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.other_guardian_a, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.admin_b = User.objects.create_user(username="admin_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_b, school=self.school_b, role=SchoolMembership.Role.ADMIN
        )

        teacher_a_membership = SchoolMembership.objects.get(
            user=self.teacher_a, school=self.school_a
        )
        self.class_a = SchoolClass.objects.create(
            school=self.school_a, name="Grade 4", academic_year="2026-2027",
            homeroom_teacher=teacher_a_membership,
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school_b, name="Grade 4", academic_year="2026-2027"
        )

        self.student = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        self.other_student = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ben", last_name="Cruz"
        )
        GuardianLink.objects.create(guardian=self.guardian_a, student=self.student)
        GuardianLink.objects.create(guardian=self.second_guardian_a, student=self.student)
        GuardianLink.objects.create(guardian=self.other_guardian_a, student=self.other_student)

        self.term = Term.objects.create(
            school=self.school_a, name="Term 1",
            start_date=date(2026, 6, 1), end_date=date(2026, 8, 31),
        )

    def _entry_formset_payload(self, subjects_and_grades, prefix="entries"):
        data = {
            f"{prefix}-TOTAL_FORMS": str(len(subjects_and_grades)),
            f"{prefix}-INITIAL_FORMS": "0",
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        for i, (subject, grade) in enumerate(subjects_and_grades):
            data[f"{prefix}-{i}-subject"] = subject
            data[f"{prefix}-{i}-grade"] = grade
            data[f"{prefix}-{i}-order"] = str(i)
        return data

    def test_model_unique_constraint_per_student_per_term(self):
        ReportCard.objects.create(student=self.student, term=self.term, school_class=self.class_a)
        with self.assertRaises(Exception):
            ReportCard.objects.create(
                student=self.student, term=self.term, school_class=self.class_a
            )

    def test_admin_can_create_term_teacher_and_guardian_cannot(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:term_new"),
            {"name": "Term 2", "start_date": "2026-09-01", "end_date": "2026-11-30"},
        )
        self.assertRedirects(response, reverse("core:terms"))
        self.assertTrue(Term.objects.filter(school=self.school_a, name="Term 2").exists())

        for username in ("teacher_a", "guardian_a"):
            self.client.login(username=username, password="pw12345!")
            response = self.client.get(reverse("core:term_new"))
            self.assertEqual(response.status_code, 403)
            self.client.logout()

    def test_homeroom_teacher_can_enter_report_for_own_class(self):
        self.client.login(username="teacher_a", password="pw12345!")
        payload = {"comment": "Doing well this term."}
        payload.update(self._entry_formset_payload([("Mathematics", "A"), ("Science", "B+")]))
        payload["save_draft"] = "1"
        response = self.client.post(
            reverse("core:report_card_edit", args=[self.class_a.pk, self.term.pk, self.student.pk]),
            payload,
        )
        self.assertRedirects(
            response,
            f"{reverse('core:report_cards_roster', args=[self.class_a.pk])}?term={self.term.pk}",
        )
        report = ReportCard.objects.get(student=self.student, term=self.term)
        self.assertEqual(report.status, ReportCard.Status.DRAFT)
        self.assertEqual(report.comment, "Doing well this term.")
        self.assertEqual(report.entries.count(), 2)
        self.assertEqual(report.created_by, self.teacher_a)

    def test_teacher_not_teaching_the_class_is_blocked(self):
        self.client.login(username="other_teacher_a", password="pw12345!")
        response = self.client.get(
            reverse("core:report_card_edit", args=[self.class_a.pk, self.term.pk, self.student.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_can_enter_report_on_teachers_behalf(self):
        self.client.login(username="admin_a", password="pw12345!")
        payload = {"comment": "Entered by admin."}
        payload.update(self._entry_formset_payload([("Mathematics", "A")]))
        payload["save_draft"] = "1"
        response = self.client.post(
            reverse("core:report_card_edit", args=[self.class_a.pk, self.term.pk, self.student.pk]),
            payload,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ReportCard.objects.filter(student=self.student, term=self.term).exists())

    def test_admin_cannot_reach_other_schools_class(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(
            reverse("core:report_cards_roster", args=[self.class_b.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_publish_sets_status_and_makes_it_visible_to_all_linked_guardians(self):
        self.client.login(username="teacher_a", password="pw12345!")
        payload = {"comment": "Great progress."}
        payload.update(self._entry_formset_payload([("Mathematics", "A")]))
        payload["publish"] = "1"
        self.client.post(
            reverse("core:report_card_edit", args=[self.class_a.pk, self.term.pk, self.student.pk]),
            payload,
        )
        report = ReportCard.objects.get(student=self.student, term=self.term)
        self.assertEqual(report.status, ReportCard.Status.PUBLISHED)
        self.assertIsNotNone(report.published_at)

        # Both guardians linked to this student (multi-guardian household)
        # see the same published report.
        for username in ("guardian_a", "second_guardian_a"):
            self.client.login(username=username, password="pw12345!")
            response = self.client.get(reverse("core:my_child_report_cards", args=[self.student.pk]))
            self.assertContains(response, "Term 1")
            view_response = self.client.get(reverse("core:report_card_view", args=[report.pk]))
            self.assertEqual(view_response.status_code, 200)
            self.assertContains(view_response, "Mathematics")
            self.client.logout()

        self.assertTrue(
            ReportCardRead.objects.filter(report_card=report, guardian=self.guardian_a).exists()
        )

    def test_draft_not_visible_to_guardian(self):
        report = ReportCard.objects.create(
            student=self.student, term=self.term, school_class=self.class_a,
            status=ReportCard.Status.DRAFT, created_by=self.teacher_a,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_report_cards", args=[self.student.pk]))
        self.assertNotContains(response, "Term 1")

        response = self.client.get(reverse("core:report_card_view", args=[report.pk]))
        self.assertEqual(response.status_code, 403)

    def test_guardian_cannot_view_another_familys_child_report(self):
        report = ReportCard.objects.create(
            student=self.other_student, term=self.term, school_class=self.class_a,
            status=ReportCard.Status.PUBLISHED, published_at=timezone.now(),
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:report_card_view", args=[report.pk]))
        self.assertEqual(response.status_code, 403)
        response = self.client.get(
            reverse("core:my_child_report_cards", args=[self.other_student.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_publish_all_drafts_bulk_action(self):
        ReportCard.objects.create(
            student=self.student, term=self.term, school_class=self.class_a,
            status=ReportCard.Status.DRAFT, created_by=self.teacher_a,
        )
        ReportCard.objects.create(
            student=self.other_student, term=self.term, school_class=self.class_a,
            status=ReportCard.Status.DRAFT, created_by=self.teacher_a,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:report_cards_publish_all", args=[self.class_a.pk, self.term.pk])
        )
        self.assertRedirects(
            response,
            f"{reverse('core:report_cards_roster', args=[self.class_a.pk])}?term={self.term.pk}",
        )
        statuses = set(
            ReportCard.objects.filter(school_class=self.class_a, term=self.term).values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, {ReportCard.Status.PUBLISHED})

    def test_guardian_cannot_reach_teacher_or_admin_screens(self):
        self.client.login(username="guardian_a", password="pw12345!")
        self.assertEqual(
            self.client.get(reverse("core:report_cards_classes")).status_code, 403
        )
        self.assertEqual(
            self.client.get(reverse("core:report_cards_roster", args=[self.class_a.pk])).status_code,
            403,
        )
        self.assertEqual(self.client.get(reverse("core:terms")).status_code, 403)

    def test_attendance_summary_pulled_into_report_form(self):
        AttendanceRecord.objects.create(
            student=self.student, school_class=self.class_a, date=date(2026, 6, 5),
            status=AttendanceRecord.Status.PRESENT,
        )
        AttendanceRecord.objects.create(
            student=self.student, school_class=self.class_a, date=date(2026, 6, 6),
            status=AttendanceRecord.Status.ABSENT,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(
            reverse("core:report_card_edit", args=[self.class_a.pk, self.term.pk, self.student.pk])
        )
        self.assertEqual(response.context["attendance_summary"], {"present": 1, "absent": 1, "late": 0})

    def test_subject_list_copied_from_sibling_report_in_same_class_and_term(self):
        first_report = ReportCard.objects.create(
            student=self.student, term=self.term, school_class=self.class_a, created_by=self.teacher_a
        )
        ReportCardEntry.objects.create(report_card=first_report, subject="Mathematics", grade="A", order=0)
        ReportCardEntry.objects.create(report_card=first_report, subject="Science", grade="B+", order=1)

        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(
            reverse(
                "core:report_card_edit",
                args=[self.class_a.pk, self.term.pk, self.other_student.pk],
            )
        )
        self.assertContains(response, "Mathematics")
        self.assertContains(response, "Science")
        # Second student's own new report has no entries saved yet — only
        # the subject names were copied as unsaved initial form data.
        self.assertEqual(
            ReportCard.objects.get(student=self.other_student, term=self.term).entries.count(), 0
        )


class MessagingTests(TestCase):
    """
    Covers the Guardian ↔ Teacher Messaging feature: School.
    messaging_enabled, core.models.MessageThread/Message, and
    core.views.messages_inbox/message_thread_start/
    message_thread_detail. The central permission boundary: a thread
    can only be created between a guardian actually linked to a student
    (GuardianLink) and a teacher actually teaching that student's class
    (homeroom or co-teacher) — never an open directory of arbitrary
    staff — and the whole feature is off by default per school.
    """

    def setUp(self):
        self.school_a = School.objects.create(
            name="School A", country="PH", messaging_enabled=True
        )
        self.school_b = School.objects.create(
            name="School B", country="PH", messaging_enabled=True
        )

        self.admin_a = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin_a, school=self.school_a, role=SchoolMembership.Role.ADMIN
        )

        self.teacher_user = User.objects.create_user(username="teacher_a", password="pw12345!")
        self.teacher_membership = SchoolMembership.objects.create(
            user=self.teacher_user, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )
        self.other_teacher_user = User.objects.create_user(
            username="teacher_other", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.other_teacher_user, school=self.school_a, role=SchoolMembership.Role.TEACHER
        )

        self.guardian_user = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_user, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )
        self.unrelated_guardian = User.objects.create_user(
            username="guardian_z", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.unrelated_guardian, school=self.school_a, role=SchoolMembership.Role.GUARDIAN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school_a,
            name="Grade 4 - Sampaguita",
            academic_year="2026-2027",
            homeroom_teacher=self.teacher_membership,
        )
        self.student = Student.objects.create(
            school=self.school_a, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        GuardianLink.objects.create(guardian=self.guardian_user, student=self.student)

    def test_messaging_disabled_by_default(self):
        school = School.objects.create(name="Fresh school", country="PH")
        self.assertFalse(school.messaging_enabled)

    def test_inbox_still_accessible_when_messaging_disabled(self):
        """The inbox itself isn't gated on the switch (proposal:
        'Per-School Messaging On/Off Switch' — existing threads stay
        readable) — only new threads/messages are blocked. Confirmed
        properly (with an actual existing thread, still listed) in
        MessagingToggleTests; this just checks the page doesn't 404 or
        redirect a guardian away from their own history."""
        self.school_a.messaging_enabled = False
        self.school_a.save(update_fields=["messaging_enabled"])
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:messages"))
        self.assertEqual(response.status_code, 200)

    def test_thread_start_blocked_when_messaging_disabled(self):
        self.school_a.messaging_enabled = False
        self.school_a.save(update_fields=["messaging_enabled"])
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.teacher_user.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_guardian_can_start_thread_with_connected_teacher(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.teacher_user.pk])
        )
        thread = MessageThread.objects.get(
            guardian=self.guardian_user, teacher=self.teacher_user, student=self.student
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))

    def test_guardian_cannot_start_thread_with_unconnected_teacher(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(
            reverse(
                "core:message_thread_start", args=[self.student.pk, self.other_teacher_user.pk]
            )
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            MessageThread.objects.filter(
                guardian=self.guardian_user, teacher=self.other_teacher_user
            ).exists()
        )

    def test_unrelated_guardian_cannot_start_thread_about_someone_elses_child(self):
        self.client.login(username="guardian_z", password="pw12345!")
        response = self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.teacher_user.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_teacher_can_start_thread_with_connected_guardian(self):
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.guardian_user.pk])
        )
        thread = MessageThread.objects.get(
            guardian=self.guardian_user, teacher=self.teacher_user, student=self.student
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))

    def test_unconnected_teacher_cannot_start_thread(self):
        self.client.login(username="teacher_other", password="pw12345!")
        response = self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.guardian_user.pk])
        )
        self.assertEqual(response.status_code, 404)

    def test_thread_is_reused_not_duplicated(self):
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.teacher_user.pk])
        )
        self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.teacher_user.pk])
        )
        self.assertEqual(
            MessageThread.objects.filter(
                guardian=self.guardian_user, teacher=self.teacher_user, student=self.student
            ).count(),
            1,
        )

    def test_guardian_can_send_and_teacher_sees_it_and_it_gets_marked_read(self):
        thread = MessageThread.objects.create(
            school=self.school_a,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]),
            {"body": "Can she stay for lunch club today?"},
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))
        message = Message.objects.get(thread=thread)
        self.assertEqual(message.sender, self.guardian_user)
        self.assertEqual(message.body, "Can she stay for lunch club today?")
        self.assertIsNone(message.read_at)
        thread.refresh_from_db()
        self.assertIsNotNone(thread.last_message_at)

        self.client.logout()
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertContains(response, "Can she stay for lunch club today?")
        message.refresh_from_db()
        self.assertIsNotNone(message.read_at)

    def test_own_message_not_marked_read_by_own_view(self):
        thread = MessageThread.objects.create(
            school=self.school_a,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        message = Message.objects.create(thread=thread, sender=self.guardian_user, body="Hi")
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        message.refresh_from_db()
        self.assertIsNone(message.read_at)

    def test_rejects_empty_message(self):
        thread = MessageThread.objects.create(
            school=self.school_a,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.post(reverse("core:message_thread_detail", args=[thread.pk]), {"body": "   "})
        self.assertFalse(Message.objects.filter(thread=thread).exists())

    def test_teacher_cannot_view_another_teachers_thread(self):
        thread = MessageThread.objects.create(
            school=self.school_a,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        self.client.login(username="teacher_other", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 404)

    def test_guardian_cannot_view_another_guardians_thread(self):
        thread = MessageThread.objects.create(
            school=self.school_a,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        self.client.login(username="guardian_z", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 404)

    def test_inbox_shows_unread_count(self):
        thread = MessageThread.objects.create(
            school=self.school_a,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        Message.objects.create(thread=thread, sender=self.guardian_user, body="Hello")
        Message.objects.create(thread=thread, sender=self.guardian_user, body="Are you there?")
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:messages"))
        threads = response.context["threads"]
        self.assertEqual(threads[0].unread_count, 2)

    def test_admin_cannot_start_or_view_threads(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.guardian_user.pk])
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.get(reverse("core:messages"))
        self.assertEqual(response.status_code, 403)

    def test_settings_form_can_toggle_messaging_off(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:settings"),
            {
                "name": "School A",
                "country": "PH",
                "default_language": "en",
                "timezone": "Asia/Manila",
                "academic_year_start_month": 6,
                # messaging_enabled deliberately omitted — an unchecked
                # checkbox simply isn't present in POST data.
            },
        )
        self.assertRedirects(response, reverse("core:settings"))
        self.school_a.refresh_from_db()
        self.assertFalse(self.school_a.messaging_enabled)


class ClassMessagingTests(TestCase):
    """
    Covers Class Group Messaging: the CLASS-type MessageThread that's
    automatically created and kept in sync for a class (core.messaging.
    sync_class_thread, wired up via core.signals), teacher moderation
    (removing a message leaves a placeholder), the per-class
    announcements-only toggle, and how it shows up in the shared
    messages_inbox/message_thread_detail views alongside one-to-one
    threads.
    """

    def setUp(self):
        self.school = School.objects.create(
            name="School A", country="PH", messaging_enabled=True
        )
        self.admin = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin, school=self.school, role=SchoolMembership.Role.ADMIN
        )

        self.teacher_user = User.objects.create_user(
            username="teacher_a", first_name="Tina", last_name="Teach", password="pw12345!"
        )
        self.teacher_membership = SchoolMembership.objects.create(
            user=self.teacher_user, school=self.school, role=SchoolMembership.Role.TEACHER
        )
        self.co_teacher_user = User.objects.create_user(
            username="teacher_co", first_name="Cory", last_name="Coach", password="pw12345!"
        )
        self.co_teacher_membership = SchoolMembership.objects.create(
            user=self.co_teacher_user, school=self.school, role=SchoolMembership.Role.TEACHER
        )
        self.other_teacher_user = User.objects.create_user(
            username="teacher_other", password="pw12345!"
        )
        self.other_teacher_membership = SchoolMembership.objects.create(
            user=self.other_teacher_user, school=self.school, role=SchoolMembership.Role.TEACHER
        )

        self.guardian_user = User.objects.create_user(
            username="guardian_a", first_name="Gina", last_name="Guardian", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.guardian_user, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        self.other_guardian_user = User.objects.create_user(
            username="guardian_b", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.other_guardian_user, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        self.unrelated_guardian = User.objects.create_user(
            username="guardian_z", password="pw12345!"
        )
        SchoolMembership.objects.create(
            user=self.unrelated_guardian, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )

        self.class_a = SchoolClass.objects.create(
            school=self.school,
            name="Grade 4 - Sampaguita",
            academic_year="2026-2027",
            homeroom_teacher=self.teacher_membership,
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school,
            name="Grade 5 - Rosal",
            academic_year="2026-2027",
        )
        self.student = Student.objects.create(
            school=self.school, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        self.guardian_link = GuardianLink.objects.create(
            guardian=self.guardian_user, student=self.student
        )

    def _class_thread(self, school_class=None):
        return MessageThread.objects.get(
            thread_type=MessageThread.ThreadType.CLASS,
            school_class=school_class or self.class_a,
        )

    # -- creation / auto-sync -------------------------------------------------

    def test_class_thread_created_once_class_has_a_student(self):
        thread = self._class_thread()
        self.assertEqual(thread.school, self.school)
        participant_ids = set(thread.participants.values_list("id", flat=True))
        self.assertEqual(participant_ids, {self.teacher_user.id, self.guardian_user.id})

    def test_no_thread_for_class_with_no_students(self):
        self.assertFalse(
            MessageThread.objects.filter(
                thread_type=MessageThread.ThreadType.CLASS, school_class=self.class_b
            ).exists()
        )

    def test_no_thread_created_when_messaging_disabled(self):
        school = School.objects.create(name="Off school", country="PH")
        klass = SchoolClass.objects.create(
            school=school, name="Grade 1", academic_year="2026-2027"
        )
        Student.objects.create(school=school, school_class=klass, first_name="A", last_name="B")
        self.assertFalse(
            MessageThread.objects.filter(
                thread_type=MessageThread.ThreadType.CLASS, school_class=klass
            ).exists()
        )

    def test_guardian_added_when_child_enrolled(self):
        student2 = Student.objects.create(
            school=self.school, school_class=self.class_a, first_name="Bea", last_name="Cruz"
        )
        GuardianLink.objects.create(guardian=self.other_guardian_user, student=student2)
        thread = self._class_thread()
        self.assertIn(self.other_guardian_user, thread.participants.all())

    def test_guardian_removed_when_guardian_link_removed(self):
        self.guardian_link.delete()
        thread = self._class_thread()
        self.assertNotIn(self.guardian_user, thread.participants.all())

    def test_guardian_moved_off_and_on_thread_on_mid_term_transfer(self):
        """A student transferring classes mid-term should add/remove
        thread access immediately, not leave stale access behind
        (proposal: 'Testing & rollout' edge case)."""
        Student.objects.create(
            school=self.school, school_class=self.class_b, first_name="X", last_name="Y"
        )  # give class_b a student so it gets its own thread first
        self.student.school_class = self.class_b
        self.student.save()

        old_thread = self._class_thread(self.class_a)
        new_thread = self._class_thread(self.class_b)
        self.assertNotIn(self.guardian_user, old_thread.participants.all())
        self.assertIn(self.guardian_user, new_thread.participants.all())

    def test_teacher_added_and_removed_on_homeroom_change(self):
        self.class_a.homeroom_teacher = self.other_teacher_membership
        self.class_a.save()
        thread = self._class_thread()
        self.assertNotIn(self.teacher_user, thread.participants.all())
        self.assertIn(self.other_teacher_user, thread.participants.all())

    def test_teacher_added_via_additional_teachers(self):
        self.class_a.additional_teachers.add(self.co_teacher_membership)
        thread = self._class_thread()
        self.assertIn(self.co_teacher_user, thread.participants.all())

    def test_teacher_removed_via_additional_teachers(self):
        self.class_a.additional_teachers.add(self.co_teacher_membership)
        self.class_a.additional_teachers.remove(self.co_teacher_membership)
        thread = self._class_thread()
        self.assertNotIn(self.co_teacher_user, thread.participants.all())

    def test_class_threads_backfilled_when_school_messaging_turned_on(self):
        school = School.objects.create(name="Freshly on", country="PH")
        membership = SchoolMembership.objects.create(
            user=self.teacher_user, school=school, role=SchoolMembership.Role.TEACHER
        )
        klass = SchoolClass.objects.create(
            school=school, name="Grade 2", academic_year="2026-2027", homeroom_teacher=membership
        )
        Student.objects.create(school=school, school_class=klass, first_name="A", last_name="B")
        self.assertFalse(
            MessageThread.objects.filter(
                thread_type=MessageThread.ThreadType.CLASS, school_class=klass
            ).exists()
        )
        school.messaging_enabled = True
        school.save()
        self.assertTrue(
            MessageThread.objects.filter(
                thread_type=MessageThread.ThreadType.CLASS, school_class=klass
            ).exists()
        )

    def test_class_thread_survives_last_student_leaving(self):
        """History stays even once every student has left — only
        participants shrink, per core.messaging.sync_class_thread's
        docstring."""
        self.student.school_class = None
        self.student.save()
        self.assertTrue(
            MessageThread.objects.filter(
                thread_type=MessageThread.ThreadType.CLASS, school_class=self.class_a
            ).exists()
        )
        thread = self._class_thread()
        self.assertNotIn(self.guardian_user, thread.participants.all())
        self.assertIn(self.teacher_user, thread.participants.all())

    # -- access ----------------------------------------------------------------

    def test_participant_can_view_thread(self):
        thread = self._class_thread()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 200)

    def test_non_participant_guardian_cannot_view_thread(self):
        thread = self._class_thread()
        self.client.login(username="guardian_z", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 404)

    def test_unconnected_teacher_cannot_view_thread(self):
        thread = self._class_thread()
        self.client.login(username="teacher_other", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 404)

    def test_thread_still_viewable_read_only_when_messaging_turned_off(self):
        """Proposal: 'Per-School Messaging On/Off Switch' — turning
        messaging off doesn't hide or delete an existing thread, it
        becomes read-only. Posting-blocked behavior is covered by
        MessagingToggleTests; this just confirms the participant can
        still open and read it."""
        thread = self._class_thread()
        self.school.messaging_enabled = False
        self.school.save()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 200)

    def test_admin_has_no_class_thread_access(self):
        thread = self._class_thread()
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 404)

    # -- posting -----------------------------------------------------------------

    def test_guardian_can_post_by_default(self):
        thread = self._class_thread()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Hi everyone!"}
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertTrue(Message.objects.filter(thread=thread, body="Hi everyone!").exists())

    def test_teacher_can_post(self):
        thread = self._class_thread()
        self.client.login(username="teacher_a", password="pw12345!")
        self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Reminder: field trip Friday."}
        )
        self.assertTrue(Message.objects.filter(thread=thread, sender=self.teacher_user).exists())

    def test_guardian_cannot_post_when_announcements_only(self):
        thread = self._class_thread()
        thread.announcements_only = True
        thread.save(update_fields=["announcements_only"])
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Can I post?"}
        )
        self.assertFalse(Message.objects.filter(thread=thread, body="Can I post?").exists())

    def test_teacher_can_still_post_when_announcements_only(self):
        thread = self._class_thread()
        thread.announcements_only = True
        thread.save(update_fields=["announcements_only"])
        self.client.login(username="teacher_a", password="pw12345!")
        self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Announcement."}
        )
        self.assertTrue(Message.objects.filter(thread=thread, body="Announcement.").exists())

    # -- announcements-only toggle -------------------------------------------

    def test_teacher_can_toggle_announcements_only(self):
        thread = self._class_thread()
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:class_thread_toggle_announcements_only", args=[thread.pk])
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))
        thread.refresh_from_db()
        self.assertTrue(thread.announcements_only)

        self.client.post(reverse("core:class_thread_toggle_announcements_only", args=[thread.pk]))
        thread.refresh_from_db()
        self.assertFalse(thread.announcements_only)

    def test_guardian_cannot_toggle_announcements_only(self):
        thread = self._class_thread()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:class_thread_toggle_announcements_only", args=[thread.pk])
        )
        self.assertEqual(response.status_code, 403)
        thread.refresh_from_db()
        self.assertFalse(thread.announcements_only)

    # -- moderation --------------------------------------------------------------

    def test_teacher_can_remove_message(self):
        thread = self._class_thread()
        message = Message.objects.create(
            thread=thread, sender=self.guardian_user, body="Something inappropriate"
        )
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:class_message_remove", args=[thread.pk, message.pk])
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))
        message.refresh_from_db()
        self.assertIsNotNone(message.removed_at)
        self.assertEqual(message.removed_by, self.teacher_user)
        # the row survives — this is a placeholder, not a delete.
        self.assertTrue(Message.objects.filter(pk=message.pk).exists())

    def test_removed_message_shows_placeholder_not_body(self):
        thread = self._class_thread()
        message = Message.objects.create(
            thread=thread, sender=self.guardian_user, body="Something inappropriate"
        )
        message.removed_at = timezone.now()
        message.removed_by = self.teacher_user
        message.save(update_fields=["removed_at", "removed_by"])

        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertNotContains(response, "Something inappropriate")
        self.assertContains(response, "Message removed")

    def test_guardian_cannot_remove_message(self):
        thread = self._class_thread()
        message = Message.objects.create(thread=thread, sender=self.guardian_user, body="Hi")
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:class_message_remove", args=[thread.pk, message.pk])
        )
        self.assertEqual(response.status_code, 403)
        message.refresh_from_db()
        self.assertIsNone(message.removed_at)

    def test_one_to_one_thread_has_no_class_moderation(self):
        one_to_one = MessageThread.objects.create(
            school=self.school,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        message = Message.objects.create(thread=one_to_one, sender=self.guardian_user, body="Hi")
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:class_message_remove", args=[one_to_one.pk, message.pk])
        )
        self.assertEqual(response.status_code, 404)

    # -- unread tracking / inbox --------------------------------------------

    def test_class_thread_unread_count_based_on_thread_read_state(self):
        thread = self._class_thread()
        Message.objects.create(thread=thread, sender=self.guardian_user, body="One")
        Message.objects.create(thread=thread, sender=self.guardian_user, body="Two")
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:messages"))
        class_rows = [t for t in response.context["threads"] if t.thread_type == "class"]
        self.assertEqual(len(class_rows), 1)
        self.assertEqual(class_rows[0].unread_count, 2)

        # Viewing the thread records a read state...
        self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertTrue(
            MessageThreadRead.objects.filter(thread=thread, user=self.teacher_user).exists()
        )
        # ...so a message sent before that moment is no longer unread.
        response = self.client.get(reverse("core:messages"))
        class_rows = [t for t in response.context["threads"] if t.thread_type == "class"]
        self.assertEqual(class_rows[0].unread_count, 0)

    def test_removed_message_never_counts_as_unread(self):
        thread = self._class_thread()
        message = Message.objects.create(thread=thread, sender=self.guardian_user, body="Hi")
        message.removed_at = timezone.now()
        message.save(update_fields=["removed_at"])
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:messages"))
        class_rows = [t for t in response.context["threads"] if t.thread_type == "class"]
        self.assertEqual(class_rows[0].unread_count, 0)

    def test_inbox_combines_one_to_one_and_class_threads(self):
        class_thread = self._class_thread()
        one_to_one = MessageThread.objects.create(
            school=self.school,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:messages"))
        thread_ids = {t.pk for t in response.context["threads"]}
        self.assertEqual(thread_ids, {class_thread.pk, one_to_one.pk})

    def test_unrelated_guardian_does_not_see_class_thread_in_inbox(self):
        self.client.login(username="guardian_z", password="pw12345!")
        response = self.client.get(reverse("core:messages"))
        self.assertEqual(len(response.context["threads"]), 0)


class MessagingToggleTests(TestCase):
    """
    Covers the Per-School Messaging On/Off Switch: SchoolClass.
    messaging_enabled (the teacher-level override, opt-out, default
    True), core.messaging.class_messaging_effectively_enabled/
    thread_messaging_enabled ("no partial state" — a class switch only
    means something once the school switch is on), and the "clean
    disable, not deletion" rule — new threads/messages are blocked at
    the permission layer, but an existing thread stays viewable
    read-only rather than disappearing.
    """

    def setUp(self):
        self.school = School.objects.create(
            name="School A", country="PH", messaging_enabled=True
        )
        self.admin = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin, school=self.school, role=SchoolMembership.Role.ADMIN
        )
        self.teacher_user = User.objects.create_user(username="teacher_a", password="pw12345!")
        self.teacher_membership = SchoolMembership.objects.create(
            user=self.teacher_user, school=self.school, role=SchoolMembership.Role.TEACHER
        )
        self.guardian_user = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_user, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        self.class_a = SchoolClass.objects.create(
            school=self.school,
            name="Grade 4 - Sampaguita",
            academic_year="2026-2027",
            homeroom_teacher=self.teacher_membership,
        )
        self.student = Student.objects.create(
            school=self.school, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        GuardianLink.objects.create(guardian=self.guardian_user, student=self.student)

    # -- model default -----------------------------------------------------

    def test_class_messaging_enabled_defaults_true(self):
        klass = SchoolClass.objects.create(
            school=self.school, name="Grade 5", academic_year="2026-2027"
        )
        self.assertTrue(klass.messaging_enabled)

    # -- effective-enabled helpers -------------------------------------------

    def test_effectively_enabled_when_both_switches_on(self):
        self.assertTrue(class_messaging_effectively_enabled(self.class_a))

    def test_not_effectively_enabled_when_school_off(self):
        self.school.messaging_enabled = False
        self.school.save()
        self.class_a.refresh_from_db()
        self.assertTrue(self.class_a.messaging_enabled)  # class flag itself untouched
        self.assertFalse(class_messaging_effectively_enabled(self.class_a))

    def test_not_effectively_enabled_when_class_opts_out(self):
        self.class_a.messaging_enabled = False
        self.class_a.save()
        self.assertFalse(class_messaging_effectively_enabled(self.class_a))

    def test_not_effectively_enabled_for_no_class(self):
        self.assertFalse(class_messaging_effectively_enabled(None))

    def test_thread_messaging_enabled_for_class_thread(self):
        thread_pk = MessageThread.objects.get(
            thread_type=MessageThread.ThreadType.CLASS, school_class=self.class_a
        ).pk
        self.assertTrue(
            thread_messaging_enabled(MessageThread.objects.get(pk=thread_pk))
        )
        self.class_a.messaging_enabled = False
        self.class_a.save()
        # Re-fetched fresh, not reusing the same Python object above — a
        # ForeignKey access caches the related row on that instance, which
        # would otherwise mask the update we just made via a different
        # (self.class_a) object, something a real request never does since
        # every view builds its thread object fresh from the database.
        self.assertFalse(
            thread_messaging_enabled(MessageThread.objects.get(pk=thread_pk))
        )

    def test_thread_messaging_enabled_for_one_to_one_thread_follows_students_class(self):
        thread = MessageThread.objects.create(
            school=self.school,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        self.assertTrue(thread_messaging_enabled(thread))
        self.class_a.messaging_enabled = False
        self.class_a.save()
        self.assertFalse(thread_messaging_enabled(thread))
        self.class_a.messaging_enabled = True
        self.class_a.save()
        self.school.messaging_enabled = False
        self.school.save()
        self.assertFalse(thread_messaging_enabled(thread))

    # -- class thread creation respects the class-level switch ---------------

    def test_class_thread_not_created_when_class_opts_out_before_qualifying(self):
        klass = SchoolClass.objects.create(
            school=self.school,
            name="Grade 5",
            academic_year="2026-2027",
            messaging_enabled=False,
        )
        Student.objects.create(school=self.school, school_class=klass, first_name="A", last_name="B")
        self.assertFalse(
            MessageThread.objects.filter(
                thread_type=MessageThread.ThreadType.CLASS, school_class=klass
            ).exists()
        )

    def test_class_thread_created_once_class_opts_back_in(self):
        klass = SchoolClass.objects.create(
            school=self.school,
            name="Grade 5",
            academic_year="2026-2027",
            messaging_enabled=False,
        )
        Student.objects.create(school=self.school, school_class=klass, first_name="A", last_name="B")
        klass.messaging_enabled = True
        klass.save()
        self.assertTrue(
            MessageThread.objects.filter(
                thread_type=MessageThread.ThreadType.CLASS, school_class=klass
            ).exists()
        )

    def test_existing_class_thread_membership_keeps_syncing_after_class_opts_out(self):
        """Opting a class out doesn't touch its already-existing thread's
        membership or history — only whether new messages can be posted
        (core.messaging.sync_class_thread's docstring)."""
        thread = MessageThread.objects.get(
            thread_type=MessageThread.ThreadType.CLASS, school_class=self.class_a
        )
        self.class_a.messaging_enabled = False
        self.class_a.save()
        student2 = Student.objects.create(
            school=self.school, school_class=self.class_a, first_name="Bea", last_name="Cruz"
        )
        other_guardian = User.objects.create_user(username="guardian_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=other_guardian, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        GuardianLink.objects.create(guardian=other_guardian, student=student2)
        self.assertIn(other_guardian, thread.participants.all())

    # -- new-thread creation blocked -----------------------------------------

    def test_message_thread_start_blocked_when_class_opts_out(self):
        self.class_a.messaging_enabled = False
        self.class_a.save()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(
            reverse("core:message_thread_start", args=[self.student.pk, self.teacher_user.pk])
        )
        self.assertEqual(response.status_code, 404)

    # -- existing threads stay read-only, not hidden -------------------------

    def test_class_thread_viewable_but_not_postable_when_class_opts_out(self):
        thread = MessageThread.objects.get(
            thread_type=MessageThread.ThreadType.CLASS, school_class=self.class_a
        )
        self.class_a.messaging_enabled = False
        self.class_a.save()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_post"])

        response = self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Hello?"}
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertFalse(Message.objects.filter(thread=thread, body="Hello?").exists())

    def test_one_to_one_thread_viewable_but_not_postable_when_school_off(self):
        thread = MessageThread.objects.create(
            school=self.school,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        Message.objects.create(thread=thread, sender=self.guardian_user, body="Earlier message")
        self.school.messaging_enabled = False
        self.school.save()

        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:message_thread_detail", args=[thread.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Earlier message")
        self.assertFalse(response.context["can_post"])

        response = self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]), {"body": "New message"}
        )
        self.assertFalse(Message.objects.filter(thread=thread, body="New message").exists())

    def test_inbox_lists_existing_threads_when_school_off(self):
        thread = MessageThread.objects.create(
            school=self.school,
            student=self.student,
            guardian=self.guardian_user,
            teacher=self.teacher_user,
        )
        self.school.messaging_enabled = False
        self.school.save()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:messages"))
        self.assertEqual(response.status_code, 200)
        thread_ids = {t.pk for t in response.context["threads"]}
        self.assertIn(thread.pk, thread_ids)
        self.assertFalse(response.context["messaging_enabled"])

    # -- moderation isn't blocked by the switch ------------------------------

    def test_teacher_can_still_remove_message_when_messaging_off(self):
        thread = MessageThread.objects.get(
            thread_type=MessageThread.ThreadType.CLASS, school_class=self.class_a
        )
        message = Message.objects.create(thread=thread, sender=self.guardian_user, body="Hi")
        self.school.messaging_enabled = False
        self.school.save()
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.post(
            reverse("core:class_message_remove", args=[thread.pk, message.pk])
        )
        self.assertRedirects(response, reverse("core:message_thread_detail", args=[thread.pk]))
        message.refresh_from_db()
        self.assertIsNotNone(message.removed_at)

    # -- entry points hidden, not just the composer --------------------------

    def test_class_group_chat_button_hidden_on_class_detail_when_class_opts_out(self):
        self.class_a.messaging_enabled = False
        self.class_a.save()
        self.client.login(username="teacher_a", password="pw12345!")
        response = self.client.get(reverse("core:class_detail", args=[self.class_a.pk]))
        self.assertIsNone(response.context["class_thread"])

    def test_message_button_hidden_on_my_child_detail_when_class_opts_out(self):
        self.class_a.messaging_enabled = False
        self.class_a.save()
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:my_child_detail", args=[self.student.pk]))
        self.assertEqual(list(response.context["connected_teachers"]), [])
        self.assertIsNone(response.context["class_thread"])

    # -- no partial state: the per-class field only exists on the form -------
    # once the school switch is on ------------------------------------------

    def test_class_form_has_no_messaging_field_when_school_off(self):
        self.school.messaging_enabled = False
        self.school.save()
        form = SchoolClassForm(school=self.school)
        self.assertNotIn("messaging_enabled", form.fields)

    def test_class_form_has_messaging_field_when_school_on(self):
        form = SchoolClassForm(school=self.school)
        self.assertIn("messaging_enabled", form.fields)

    def test_posting_messaging_field_while_school_off_has_no_effect(self):
        """Even if someone POSTs messaging_enabled=on directly while the
        school switch is off, there's no bound field to receive it —
        the class keeps its existing value untouched."""
        self.school.messaging_enabled = False
        self.school.save()
        self.class_a.messaging_enabled = False
        self.class_a.save()
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(
            reverse("core:class_edit", args=[self.class_a.pk]),
            {
                "name": self.class_a.name,
                "academic_year": self.class_a.academic_year,
                "homeroom_teacher": self.teacher_membership.pk,
                "messaging_enabled": "on",
            },
        )
        self.class_a.refresh_from_db()
        self.assertFalse(self.class_a.messaging_enabled)

    # -- settings form copy ---------------------------------------------------

    def test_school_settings_page_explains_messaging_commitment(self):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.get(reverse("core:settings"))
        self.assertContains(response, "real commitment")


class PushInfrastructureTests(TestCase):
    """
    Covers the server-side plumbing of Web Push (core.models.
    PushSubscription, core.push, core.views.push_subscribe/
    push_unsubscribe/app_notifications) — the last Phase 1
    build-sequence item. Deliberately mocks pywebpush.webpush itself
    rather than hitting a real push service: what matters here is that
    Notipa calls it correctly and handles its outcomes correctly, not
    whether a real browser's push endpoint is reachable from a test run.
    """

    def setUp(self):
        self.school = School.objects.create(name="School A", country="PH")
        self.guardian = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )

    def _subscription_payload(self, endpoint="https://push.example.com/abc123"):
        return {
            "endpoint": endpoint,
            "keys": {"p256dh": "fake-p256dh-key", "auth": "fake-auth-key"},
        }

    def test_anonymous_cannot_subscribe(self):
        response = self.client.post(
            reverse("core:push_subscribe"),
            data=self._subscription_payload(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 302)  # redirected to login
        self.assertFalse(PushSubscription.objects.exists())

    def test_subscribe_creates_subscription_for_logged_in_user(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:push_subscribe"),
            data=self._subscription_payload(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        sub = PushSubscription.objects.get()
        self.assertEqual(sub.user, self.guardian)
        self.assertEqual(sub.p256dh, "fake-p256dh-key")
        self.assertEqual(sub.auth_key, "fake-auth-key")

    def test_resubscribing_same_endpoint_updates_in_place(self):
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.post(
            reverse("core:push_subscribe"),
            data=self._subscription_payload(),
            content_type="application/json",
        )
        payload = self._subscription_payload()
        payload["keys"]["auth"] = "rotated-auth-key"
        self.client.post(
            reverse("core:push_subscribe"), data=payload, content_type="application/json"
        )
        self.assertEqual(PushSubscription.objects.count(), 1)
        self.assertEqual(PushSubscription.objects.get().auth_key, "rotated-auth-key")

    def test_malformed_subscribe_payload_rejected(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.post(
            reverse("core:push_subscribe"),
            data={"nonsense": "value"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PushSubscription.objects.exists())

    def test_unsubscribe_removes_only_the_requesting_users_own_subscription(self):
        other_guardian = User.objects.create_user(username="guardian_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=other_guardian, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        PushSubscription.objects.create(
            user=self.guardian,
            endpoint="https://push.example.com/mine",
            p256dh="k1",
            auth_key="a1",
        )
        PushSubscription.objects.create(
            user=other_guardian,
            endpoint="https://push.example.com/not-mine",
            p256dh="k2",
            auth_key="a2",
        )
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.post(
            reverse("core:push_unsubscribe"),
            data={"endpoint": "https://push.example.com/not-mine"},
            content_type="application/json",
        )
        # Someone else's subscription is untouched — a guardian can only
        # ever remove their own, even if they somehow know another
        # endpoint string.
        self.assertEqual(PushSubscription.objects.count(), 2)

        self.client.post(
            reverse("core:push_unsubscribe"),
            data={"endpoint": "https://push.example.com/mine"},
            content_type="application/json",
        )
        self.assertEqual(PushSubscription.objects.count(), 1)
        self.assertFalse(PushSubscription.objects.filter(user=self.guardian).exists())

    def test_app_notifications_page_loads_for_any_role(self):
        self.client.login(username="guardian_a", password="pw12345!")
        response = self.client.get(reverse("core:app_notifications"))
        self.assertEqual(response.status_code, 200)

    def test_push_not_configured_without_vapid_keys(self):
        with override_settings(VAPID_PUBLIC_KEY="", VAPID_PRIVATE_KEY=""):
            self.assertFalse(push_configured())

    @override_settings(VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv")
    def test_push_configured_with_both_keys_set(self):
        self.assertTrue(push_configured())

    @override_settings(VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv")
    def test_dead_subscription_pruned_on_410(self):
        from pywebpush import WebPushException

        sub = PushSubscription.objects.create(
            user=self.guardian, endpoint="https://push.example.com/gone",
            p256dh="k", auth_key="a",
        )
        fake_response = mock.Mock(status_code=410)
        with mock.patch(
            "core.push.webpush", side_effect=WebPushException("gone", response=fake_response)
        ):
            result = send_push_notification(sub, "Title", "Body")
        self.assertFalse(result)
        self.assertFalse(PushSubscription.objects.filter(pk=sub.pk).exists())

    @override_settings(VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv")
    def test_successful_send_keeps_subscription_and_returns_true(self):
        sub = PushSubscription.objects.create(
            user=self.guardian, endpoint="https://push.example.com/works",
            p256dh="k", auth_key="a",
        )
        with mock.patch("core.push.webpush", return_value=mock.Mock()) as mocked:
            result = send_push_notification(sub, "Title", "Body", url="/somewhere/")
        self.assertTrue(result)
        self.assertTrue(PushSubscription.objects.filter(pk=sub.pk).exists())
        mocked.assert_called_once()
        call_kwargs = mocked.call_args.kwargs
        self.assertEqual(call_kwargs["subscription_info"]["endpoint"], sub.endpoint)


class PushNotificationTriggerTests(TestCase):
    """
    Covers every "new content" trigger that should send a push
    notification: Announcements (publish), Homework, Fee Notices,
    Permission Slips, Messaging (one-to-one and class), and Report
    Cards (publish, including the bulk publish action). Every content
    section it would notify guardians about now exists (the roadmap's
    own framing for why this feature was next), so each of those is
    checked here — not just that notify_users gets called, but that
    it's called with exactly the right recipients and no one else
    (the same cross-class/cross-school leakage concern every other
    feature in this app is tested against).

    core.views.notify_users is mocked at the point views.py imports it,
    rather than exercising the real Web Push send — this suite is about
    "does the right trigger notify the right people," which
    PushInfrastructureTests above already separately confirms actually
    sends correctly once called.
    """

    def setUp(self):
        self.school = School.objects.create(name="School A", country="PH")
        self.admin = User.objects.create_user(username="admin_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.admin, school=self.school, role=SchoolMembership.Role.ADMIN
        )
        self.teacher = User.objects.create_user(username="teacher_a", password="pw12345!")
        self.teacher_membership = SchoolMembership.objects.create(
            user=self.teacher, school=self.school, role=SchoolMembership.Role.TEACHER
        )
        self.class_a = SchoolClass.objects.create(
            school=self.school, name="Grade 4", academic_year="2026-2027",
            homeroom_teacher=self.teacher_membership,
        )
        self.class_b = SchoolClass.objects.create(
            school=self.school, name="Grade 5", academic_year="2026-2027"
        )
        self.student_a = Student.objects.create(
            school=self.school, school_class=self.class_a, first_name="Ana", last_name="Reyes"
        )
        self.student_b = Student.objects.create(
            school=self.school, school_class=self.class_b, first_name="Ben", last_name="Cruz"
        )
        self.guardian_a = User.objects.create_user(username="guardian_a", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_a, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        GuardianLink.objects.create(guardian=self.guardian_a, student=self.student_a)
        self.guardian_b = User.objects.create_user(username="guardian_b", password="pw12345!")
        SchoolMembership.objects.create(
            user=self.guardian_b, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        GuardianLink.objects.create(guardian=self.guardian_b, student=self.student_b)

    def _recipient_ids(self, mocked):
        (recipients, *_), _ = mocked.call_args
        return {u.id for u in recipients}

    @mock.patch("core.views.notify_users")
    def test_school_wide_announcement_notifies_every_guardian(self, mocked):
        self.client.login(username="admin_a", password="pw12345!")
        response = self.client.post(
            reverse("core:announcement_new"),
            {"title": "Welcome back", "body": "...", "school_class": ""},
        )
        announcement = Announcement.objects.get(title="Welcome back")
        self.client.post(reverse("core:announcement_publish", args=[announcement.pk]))
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id, self.guardian_b.id})

    @mock.patch("core.views.notify_users")
    def test_class_scoped_announcement_notifies_only_that_classs_guardians(self, mocked):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(
            reverse("core:announcement_new"),
            {"title": "Grade 4 only", "body": "...", "school_class": self.class_a.pk},
        )
        announcement = Announcement.objects.get(title="Grade 4 only")
        self.client.post(reverse("core:announcement_publish", args=[announcement.pk]))
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id})

    @mock.patch("core.views.notify_users")
    def test_homework_notifies_only_its_classs_guardians(self, mocked):
        self.client.login(username="teacher_a", password="pw12345!")
        self.client.post(
            reverse("core:homework_new"),
            {"school_class": self.class_a.pk, "title": "Reading", "description": ""},
        )
        self.assertTrue(mocked.called)
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id})

    @mock.patch("core.views.notify_users")
    def test_fee_notice_notifies_only_that_students_guardians(self, mocked):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(
            reverse("core:fee_notice_new"),
            {
                "student": self.student_a.pk, "title": "Tuition", "description": "",
                "amount": "100.00", "currency": "PHP", "due_date": "2026-09-01",
            },
        )
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id})

    @mock.patch("core.views.notify_users")
    def test_permission_slip_notifies_only_eligible_students_guardians(self, mocked):
        self.client.login(username="admin_a", password="pw12345!")
        self.client.post(
            reverse("core:permission_slip_new"),
            {"title": "Field trip", "description": "", "school_class": self.class_a.pk},
        )
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id})

    @mock.patch("core.views.notify_users")
    def test_report_card_publish_notifies_only_that_students_guardians(self, mocked):
        term = Term.objects.create(
            school=self.school, name="Term 1",
            start_date=date(2026, 6, 1), end_date=date(2026, 8, 31),
        )
        report = ReportCard.objects.create(
            student=self.student_a, term=term, school_class=self.class_a,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        data = {
            "comment": "Doing well",
            "entries-TOTAL_FORMS": "0", "entries-INITIAL_FORMS": "0",
            "entries-MIN_NUM_FORMS": "0", "entries-MAX_NUM_FORMS": "1000",
            "publish": "1",
        }
        self.client.post(
            reverse("core:report_card_edit", args=[self.class_a.pk, term.pk, self.student_a.pk]),
            data,
        )
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id})

    @mock.patch("core.views.notify_users")
    def test_publish_all_drafts_notifies_each_published_students_guardians(self, mocked):
        term = Term.objects.create(
            school=self.school, name="Term 1",
            start_date=date(2026, 6, 1), end_date=date(2026, 8, 31),
        )
        other_student = Student.objects.create(
            school=self.school, school_class=self.class_a, first_name="Cara", last_name="Dizon"
        )
        other_guardian = User.objects.create_user(username="guardian_c", password="pw12345!")
        SchoolMembership.objects.create(
            user=other_guardian, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        GuardianLink.objects.create(guardian=other_guardian, student=other_student)

        ReportCard.objects.create(
            student=self.student_a, term=term, school_class=self.class_a,
            status=ReportCard.Status.DRAFT,
        )
        ReportCard.objects.create(
            student=other_student, term=term, school_class=self.class_a,
            status=ReportCard.Status.DRAFT,
        )
        self.client.login(username="teacher_a", password="pw12345!")
        self.client.post(reverse("core:report_cards_publish_all", args=[self.class_a.pk, term.pk]))

        notified_ids = set()
        for call in mocked.call_args_list:
            (recipients, *_), _ = call
            notified_ids |= {u.id for u in recipients}
        self.assertEqual(notified_ids, {self.guardian_a.id, other_guardian.id})

    def _enable_messaging(self):
        self.school.messaging_enabled = True
        self.school.save()

    @mock.patch("core.views.notify_users")
    def test_one_to_one_message_notifies_only_the_other_party(self, mocked):
        self._enable_messaging()
        self.client.login(username="teacher_a", password="pw12345!")
        start_response = self.client.get(
            reverse("core:message_thread_start", args=[self.student_a.pk, self.guardian_a.pk])
        )
        thread = MessageThread.objects.get(
            thread_type=MessageThread.ThreadType.ONE_TO_ONE, student=self.student_a
        )
        self.client.post(reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Hi!"})
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id})

        mocked.reset_mock()
        self.client.logout()
        self.client.login(username="guardian_a", password="pw12345!")
        self.client.post(reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Hi back!"})
        self.assertEqual(self._recipient_ids(mocked), {self.teacher.id})

    @mock.patch("core.views.notify_users")
    def test_class_thread_message_notifies_every_other_participant(self, mocked):
        self._enable_messaging()
        from .messaging import sync_class_thread

        thread = sync_class_thread(self.class_a)
        self.assertIsNotNone(thread)
        self.client.login(username="teacher_a", password="pw12345!")
        self.client.post(
            reverse("core:message_thread_detail", args=[thread.pk]), {"body": "Hello class"}
        )
        self.assertEqual(self._recipient_ids(mocked), {self.guardian_a.id})
