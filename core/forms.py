from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth import password_validation

from .models import (
    Announcement,
    FeeNotice,
    GuardianLink,
    Homework,
    PermissionSlip,
    School,
    SchoolClass,
    SchoolMembership,
    Student,
)

User = get_user_model()


class SchoolForm(forms.ModelForm):
    """
    Creates a School (the tenant itself) — the in-app replacement for
    doing this via /admin/. Only reachable by superusers
    (core.permissions.superuser_required); see core.views.school_setup,
    which also creates the submitting user's own admin SchoolMembership
    so they land on a working dashboard immediately, rather than having
    to separately create that membership by hand.
    """

    class Meta:
        model = School
        fields = [
            "name",
            "country",
            "default_language",
            "timezone",
            "tier",
            "academic_year_start_month",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Sampaguita Elementary School"}
            ),
            "country": forms.TextInput(
                attrs={"class": "input", "placeholder": "ISO code, e.g. PH", "maxlength": 2}
            ),
            "default_language": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. en"}
            ),
            "timezone": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Asia/Manila"}
            ),
            "tier": forms.Select(attrs={"class": "select"}),
            "academic_year_start_month": forms.NumberInput(
                attrs={"class": "input", "min": 1, "max": 12}
            ),
        }
        help_texts = {
            "tier": "Free (Track 1, public school) or Paid (Track 2, private-school licence).",
            "academic_year_start_month": "Month the academic year starts, 1–12 (e.g. 6 for June).",
        }


class SchoolSettingsForm(forms.ModelForm):
    """
    Edits an existing School's own profile — the Admin > Settings page
    (core.views.school_settings), scoped to request.school and reachable
    by a regular school admin, not just a superuser. Deliberately a
    separate form from SchoolForm rather than the same one reused:

    - No `tier` field. Free vs Paid (Track 1 vs Track 2) is a licensing
      decision, not something a school should be able to flip on
      themselves from a settings page — that stays a platform-operator
      action via /admin/ for now, the same "not exposed in-app yet"
      category as a few other edge cases already called out elsewhere
      in this app (e.g. changing a teacher's username).
    - No way to change which School this is. The view always passes
      `instance=request.school` — there's no id field on this form at
      all, so there's nothing to tamper with in POST data the way
      SchoolClassForm/StudentForm guard against picking a different
      school's id.
    """

    class Meta:
        model = School
        fields = ["name", "country", "default_language", "timezone", "academic_year_start_month"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Sampaguita Elementary School"}
            ),
            "country": forms.TextInput(
                attrs={"class": "input", "placeholder": "ISO code, e.g. PH", "maxlength": 2}
            ),
            "default_language": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. en"}
            ),
            "timezone": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Asia/Manila"}
            ),
            "academic_year_start_month": forms.NumberInput(
                attrs={"class": "input", "min": 1, "max": 12}
            ),
        }
        help_texts = {
            "academic_year_start_month": "Month the academic year starts, 1–12 (e.g. 6 for June).",
        }


class SchoolClassForm(forms.ModelForm):
    """
    Creates a SchoolClass scoped to a specific school, passed in explicitly
    by the view (never taken from POST data — see core.views.class_new).
    homeroom_teacher is restricted to teachers already at that school, so
    this form can't be used to assign a teacher from another school.
    """

    class Meta:
        model = SchoolClass
        fields = ["name", "academic_year", "homeroom_teacher"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Grade 4 - Sampaguita"}
            ),
            "academic_year": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. 2026-2027"}
            ),
            "homeroom_teacher": forms.Select(attrs={"class": "select"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.school = school
        self.fields["homeroom_teacher"].queryset = SchoolMembership.objects.filter(
            school=school, role=SchoolMembership.Role.TEACHER, is_active=True
        ).select_related("user")
        self.fields["homeroom_teacher"].required = False
        self.fields["homeroom_teacher"].label_from_instance = (
            lambda m: m.user.get_full_name() or m.user.username
        )


class StudentForm(forms.ModelForm):
    """
    Creates a Student scoped to a specific school, passed in explicitly by
    the view (never taken from POST data — see core.views.student_new).
    school_class is restricted to classes already at that school.
    """

    class Meta:
        model = Student
        fields = ["first_name", "last_name", "date_of_birth", "student_id", "school_class"]
        widgets = {
            "first_name": forms.TextInput(attrs={"class": "input"}),
            "last_name": forms.TextInput(attrs={"class": "input"}),
            "date_of_birth": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "student_id": forms.TextInput(
                attrs={"class": "input", "placeholder": "School's own ID, optional"}
            ),
            "school_class": forms.Select(attrs={"class": "select"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.school = school
        self.fields["school_class"].queryset = SchoolClass.objects.filter(
            school=school, is_active=True
        )
        self.fields["school_class"].required = False
        self.fields["date_of_birth"].required = False
        self.fields["student_id"].required = False


class TeacherForm(forms.Form):
    """
    Creates a new User plus a SchoolMembership for a specific school,
    passed in explicitly by the view (never taken from POST data — see
    core.views.teacher_new). This is the in-app replacement for the
    /admin/-only staff invite flow flagged as the remaining onboarding
    gap in the handover plan (Section 10 / Section 9 item 2): a school
    admin can now add a teacher (or another admin) without touching
    Django admin.

    There's no email/SMS sending infrastructure yet (handover plan
    Section 10), so this can't issue an invite link the way the
    proposal's guardian flow eventually will — the admin sets an
    initial password directly here and passes it to the new teacher
    out of band. That's a deliberate, temporary trade-off, not an
    oversight.
    """

    ROLE_CHOICES = [
        (SchoolMembership.Role.TEACHER, "Teacher"),
        (SchoolMembership.Role.ADMIN, "Admin"),
    ]

    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    last_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    username = forms.CharField(
        max_length=150,
        help_text="What they'll log in with. Letters, digits, and @/./+/-/_ only.",
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "input"}),
    )
    phone_number = forms.CharField(
        max_length=32,
        required=False,
        help_text="E.164 format preferred, e.g. +639171234567.",
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "e.g. +639171234567"}),
    )
    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        initial=SchoolMembership.Role.TEACHER,
        widget=forms.Select(attrs={"class": "select"}),
    )
    password1 = forms.CharField(
        label="Initial password",
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
        help_text="Give this to the teacher directly — there's no invite email yet.",
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
    )

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError(
                "That username is already taken. To add an existing user to "
                "this school, use /admin/ for now."
            )
        return username

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2:
            if password1 != password2:
                self.add_error("password2", "Passwords don't match.")
            else:
                try:
                    password_validation.validate_password(password1)
                except forms.ValidationError as exc:
                    self.add_error("password1", exc)
        return cleaned_data

    def save(self):
        """Creates the User and the school-scoped SchoolMembership in one
        step. Requires `school` to have been passed to __init__ — the
        view is responsible for that, never trusting a school id from
        POST data (same pattern as SchoolClassForm/StudentForm)."""
        if self.school is None:
            raise ValueError("TeacherForm.save() requires a school.")

        user = User.objects.create_user(
            username=self.cleaned_data["username"],
            email=self.cleaned_data.get("email", ""),
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
            password=self.cleaned_data["password1"],
        )
        user.phone_number = self.cleaned_data.get("phone_number", "")
        user.save(update_fields=["phone_number"])

        membership = SchoolMembership.objects.create(
            user=user, school=self.school, role=self.cleaned_data["role"]
        )
        return membership


class TeacherEditForm(forms.Form):
    """
    Edits an existing teacher/admin's details (core.views.teacher_edit).
    Deliberately separate from TeacherForm rather than reusing it with
    optional password fields: editing never touches the password (there's
    no "reset password" flow yet — out of scope here) and never touches
    the username (changing a login identifier is an edge case with its
    own can of worms, e.g. session/audit-trail implications, best left
    for a later pass), so keeping the two forms apart means neither one
    has to grow conditional fields to cover the other's case.

    Archiving (is_active=False) — the soft-delete for a SchoolMembership
    — already existed as a field on the model with exactly this meaning
    ("revoke access without deleting history"); it's handled by the
    dedicated teacher_revoke/teacher_restore views instead of a checkbox
    here, matching the one-click archive/restore pattern used for
    Classes and Students.
    """

    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    last_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "input"}),
    )
    phone_number = forms.CharField(
        max_length=32,
        required=False,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "e.g. +639171234567"}),
    )
    role = forms.ChoiceField(
        choices=TeacherForm.ROLE_CHOICES,
        widget=forms.Select(attrs={"class": "select"}),
    )

    def __init__(self, *args, membership=None, **kwargs):
        self.membership = membership
        if membership is not None and not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "first_name": membership.user.first_name,
                "last_name": membership.user.last_name,
                "email": membership.user.email,
                "phone_number": membership.user.phone_number,
                "role": membership.role,
            }
        super().__init__(*args, **kwargs)

    def clean_role(self):
        role = self.cleaned_data["role"]
        if self.membership is not None and role != self.membership.role:
            conflict = (
                SchoolMembership.objects.filter(
                    user=self.membership.user, school=self.membership.school, role=role
                )
                .exclude(pk=self.membership.pk)
                .exists()
            )
            if conflict:
                raise forms.ValidationError(
                    f"{self.membership.user} already holds a {role} membership at this school."
                )
        return role

    def save(self):
        if self.membership is None:
            raise ValueError("TeacherEditForm.save() requires a membership.")

        user = self.membership.user
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.email = self.cleaned_data.get("email", "")
        user.phone_number = self.cleaned_data.get("phone_number", "")
        user.save(update_fields=["first_name", "last_name", "email", "phone_number"])

        self.membership.role = self.cleaned_data["role"]
        self.membership.save(update_fields=["role"])
        return self.membership


class GuardianForm(forms.Form):
    """
    Creates a new User plus a GUARDIAN SchoolMembership for a specific
    school, passed in explicitly by the view (never taken from POST
    data — see core.views.guardian_new). Building this in-app closes the
    same kind of gap TeacherForm closed for staff: before a guardian can
    be linked to a Student (core.models.GuardianLink — the next piece of
    work after this one), a guardian account has to exist at all.

    The proposal's long-term design (Section 4.2) is a passwordless,
    SMS-based invite for guardians specifically, since a parent
    shouldn't need an app-store account. That flow needs SMS-sending
    infrastructure this project doesn't have yet (handover plan Section
    10), so — same trade-off already made for TeacherForm — this creates
    a standard username/password account instead, and the admin/teacher
    passes the password to the guardian directly. phone_number is
    required (rather than optional, as on TeacherForm) because it's the
    field the eventual SMS invite flow will actually use, and it's worth
    capturing correctly from day one even while login still runs on
    username/password.
    """

    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    last_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    username = forms.CharField(
        max_length=150,
        help_text="What they'll log in with. Letters, digits, and @/./+/-/_ only.",
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )
    phone_number = forms.CharField(
        max_length=32,
        help_text="E.164 format preferred, e.g. +639171234567. The primary contact number.",
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "e.g. +639171234567"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "input"}),
    )
    password1 = forms.CharField(
        label="Initial password",
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
        help_text="Give this to the guardian directly — there's no invite SMS/link yet.",
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
    )

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError(
                "That username is already taken. To add an existing user to "
                "this school, use /admin/ for now."
            )
        return username

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2:
            if password1 != password2:
                self.add_error("password2", "Passwords don't match.")
            else:
                try:
                    password_validation.validate_password(password1)
                except forms.ValidationError as exc:
                    self.add_error("password1", exc)
        return cleaned_data

    def save(self):
        """Creates the User and the school-scoped GUARDIAN SchoolMembership
        in one step. Requires `school` to have been passed to __init__ —
        the view is responsible for that, never trusting a school id
        from POST data (same pattern as TeacherForm)."""
        if self.school is None:
            raise ValueError("GuardianForm.save() requires a school.")

        user = User.objects.create_user(
            username=self.cleaned_data["username"],
            email=self.cleaned_data.get("email", ""),
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
            password=self.cleaned_data["password1"],
        )
        user.phone_number = self.cleaned_data["phone_number"]
        user.save(update_fields=["phone_number"])

        membership = SchoolMembership.objects.create(
            user=user, school=self.school, role=SchoolMembership.Role.GUARDIAN
        )
        return membership


class GuardianEditForm(forms.Form):
    """
    Edits an existing guardian's details (core.views.guardian_edit).
    Separate from GuardianForm for the same reasons TeacherEditForm is
    separate from TeacherForm: no password field (no reset flow yet),
    no username field (not editable here). Unlike TeacherEditForm,
    there's no role field — a GUARDIAN membership doesn't get promoted
    to teacher/admin through this form; that's a different, deliberate
    action best done through the Teacher invite flow if it's ever
    actually needed.
    """

    first_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    last_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    phone_number = forms.CharField(
        max_length=32,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "e.g. +639171234567"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "input"}),
    )

    def __init__(self, *args, membership=None, **kwargs):
        self.membership = membership
        if membership is not None and not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "first_name": membership.user.first_name,
                "last_name": membership.user.last_name,
                "phone_number": membership.user.phone_number,
                "email": membership.user.email,
            }
        super().__init__(*args, **kwargs)

    def save(self):
        if self.membership is None:
            raise ValueError("GuardianEditForm.save() requires a membership.")

        user = self.membership.user
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.phone_number = self.cleaned_data["phone_number"]
        user.email = self.cleaned_data.get("email", "")
        user.save(update_fields=["first_name", "last_name", "phone_number", "email"])
        return self.membership


class GuardianLinkForm(forms.ModelForm):
    """
    Attaches an existing guardian (a User with an active GUARDIAN
    SchoolMembership at the student's school) to a Student — the actual
    core.models.GuardianLink record, with a relationship type and a
    primary-contact flag. This is the piece that was still missing after
    Guardian setup (core.forms.GuardianForm): a guardian account existing
    doesn't mean it's connected to any student yet.

    `student` is passed in explicitly by the view (never taken from POST
    data — see core.views.guardian_link_add), same pattern as every
    other school-scoped form here. The `guardian` dropdown is restricted
    to that student's school and excludes guardians already linked to
    this student, so this form structurally can't create a cross-school
    link or a duplicate one.
    """

    class Meta:
        model = GuardianLink
        fields = ["guardian", "relationship", "is_primary_contact"]
        widgets = {
            "guardian": forms.Select(attrs={"class": "select"}),
            "relationship": forms.Select(attrs={"class": "select"}),
        }

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.student = student
        self.instance.student = student

        already_linked_ids = []
        guardian_queryset = User.objects.none()
        if student is not None:
            already_linked_ids = student.guardian_links.values_list("guardian_id", flat=True)
            guardian_queryset = (
                User.objects.filter(
                    memberships__school=student.school,
                    memberships__role=SchoolMembership.Role.GUARDIAN,
                    memberships__is_active=True,
                )
                .exclude(pk__in=already_linked_ids)
                .distinct()
                .order_by("last_name", "first_name")
            )

        self.fields["guardian"].queryset = guardian_queryset
        self.fields["guardian"].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields["is_primary_contact"].required = False

    def clean(self):
        cleaned_data = super().clean()
        guardian = cleaned_data.get("guardian")
        if guardian is not None and self.student is not None:
            if GuardianLink.objects.filter(guardian=guardian, student=self.student).exists():
                raise forms.ValidationError("This guardian is already linked to this student.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.student = self.student
        if commit:
            instance.save()
            if instance.is_primary_contact:
                # Only one primary contact per student — demote any others
                # rather than leaving two rows both marked primary, since
                # that flag exists specifically to pick who gets SMS
                # fallback first (core.models.GuardianLink docstring).
                GuardianLink.objects.filter(student=self.student).exclude(pk=instance.pk).update(
                    is_primary_contact=False
                )
        return instance


class StudentLinkForm(forms.ModelForm):
    """
    The mirror image of GuardianLinkForm: attaches a Student to an
    existing guardian, starting from the guardian's own detail page
    (core.views.guardian_detail / student_link_add) instead of the
    student's. Same underlying core.models.GuardianLink record and the
    same constraints — this only exists because a guardian is often
    being linked to several children at once (siblings enrolling
    together), and picking students one at a time from each student's
    own page is the wrong direction to start from in that case.

    `guardian` and `school` are passed in explicitly by the view, never
    taken from POST data (same pattern as GuardianLinkForm). The
    `student` dropdown is restricted to that guardian's school, excludes
    students already linked to this guardian, and excludes archived
    students (linking a guardian to an archived student is almost
    always a mistake — the class/student dropdowns elsewhere in this
    app apply the same is_active filter).
    """

    class Meta:
        model = GuardianLink
        fields = ["student", "relationship", "is_primary_contact"]
        widgets = {
            "student": forms.Select(attrs={"class": "select"}),
            "relationship": forms.Select(attrs={"class": "select"}),
        }

    def __init__(self, *args, guardian=None, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.guardian = guardian
        self.instance.guardian = guardian

        student_queryset = Student.objects.none()
        if guardian is not None and school is not None:
            already_linked_ids = guardian.guardian_links.values_list("student_id", flat=True)
            student_queryset = (
                Student.objects.filter(school=school, is_active=True)
                .exclude(pk__in=already_linked_ids)
                .order_by("last_name", "first_name")
            )

        self.fields["student"].queryset = student_queryset
        self.fields["student"].label_from_instance = (
            lambda s: f"{s.first_name} {s.last_name}"
        )
        self.fields["is_primary_contact"].required = False

    def clean(self):
        cleaned_data = super().clean()
        student = cleaned_data.get("student")
        if student is not None and self.guardian is not None:
            if GuardianLink.objects.filter(guardian=self.guardian, student=student).exists():
                raise forms.ValidationError("This student is already linked to this guardian.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.guardian = self.guardian
        if commit:
            instance.save()
            if instance.is_primary_contact:
                GuardianLink.objects.filter(student=instance.student).exclude(pk=instance.pk).update(
                    is_primary_contact=False
                )
        return instance


class AnnouncementForm(forms.ModelForm):
    """
    Creates or edits an Announcement scoped to a specific school, passed
    in explicitly by the view (never taken from POST data — see
    core.views.announcement_new/announcement_edit), same pattern as
    every other school-scoped form here. `school_class` is restricted to
    active classes at that school; leaving it blank makes the
    announcement school-wide, per the model's own docstring.

    Deliberately doesn't include `published_at` or `author` as form
    fields — publishing is a separate explicit action (announcement_
    publish/announcement_unpublish) rather than a field on this form, so
    "save my edits" and "make this visible to guardians" can't be
    accidentally conflated into one click; `author` is set by the view
    from request.user, never something the submitter can pick.
    """

    class Meta:
        model = Announcement
        fields = ["title", "body", "school_class"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input"}),
            "body": forms.Textarea(attrs={"class": "input", "rows": 6}),
            "school_class": forms.Select(attrs={"class": "select"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.school = school
        self.fields["school_class"].queryset = SchoolClass.objects.filter(
            school=school, is_active=True
        )
        self.fields["school_class"].required = False
        self.fields["school_class"].empty_label = "School-wide (all classes)"


class HomeworkForm(forms.ModelForm):
    """
    Creates or edits a Homework item, always scoped to one class — unlike
    Announcement, the model has no school-wide option (`school_class` is
    required, not nullable), which matches how homework actually works:
    it's always assigned to a specific class, never the whole school.

    `school` is passed in explicitly by the view (never taken from POST
    data — see core.views.homework_new/homework_edit), same pattern as
    every other school-scoped form here, and is used only to restrict
    the `school_class` dropdown to that school's active classes — there
    is no direct `school` field on Homework to set on the instance.

    Unlike Announcement, there's no separate publish step: homework
    becomes visible to a class's guardians as soon as it's saved, since
    there's no draft/final distinction that makes sense for an
    assignment the way it does for a school-wide message.
    """

    class Meta:
        model = Homework
        fields = ["school_class", "title", "description", "due_date", "attachment"]
        widgets = {
            "school_class": forms.Select(attrs={"class": "select"}),
            "title": forms.TextInput(attrs={"class": "input"}),
            "description": forms.Textarea(attrs={"class": "input", "rows": 5}),
            "due_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["school_class"].queryset = SchoolClass.objects.filter(
            school=school, is_active=True
        )
        self.fields["description"].required = False
        self.fields["due_date"].required = False
        self.fields["attachment"].required = False


class FeeNoticeForm(forms.ModelForm):
    """
    Creates or edits a FeeNotice — an informational fee due-date notice,
    Track 2 / private-school only (proposal Section 4.3), always
    per-student rather than per-class or school-wide, since a fee
    (tuition, a specific charge) is something a family owes for their
    own child, not a message a whole class needs to see.

    `school` is passed in explicitly by the view (never taken from POST
    data — see core.views.fee_notice_new/fee_notice_edit), same pattern
    as every other school-scoped form here, and is used both to set the
    instance's own `school` field and to restrict the `student`
    dropdown to that school's active students — an archived student
    almost certainly shouldn't be getting a new fee notice.

    Deliberately doesn't include `status` as a form field. Like
    Announcement's `published_at`, changing a fee notice's status
    (unpaid → paid/waived, or back) is a separate, explicit action
    (core.views.fee_notice_mark_paid/mark_waived/mark_unpaid) rather than
    a field on this form — editing the amount/due date and changing
    whether it's been paid are different enough actions that conflating
    them into one form risks an accidental status change while fixing a
    typo in the description.
    """

    class Meta:
        model = FeeNotice
        fields = ["student", "title", "description", "amount", "currency", "due_date"]
        widgets = {
            "student": forms.Select(attrs={"class": "select"}),
            "title": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Term 2 tuition"}
            ),
            "description": forms.Textarea(attrs={"class": "input", "rows": 4}),
            "amount": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": 0}),
            "currency": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. PHP", "maxlength": 3}
            ),
            "due_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.school = school
        self.fields["student"].queryset = Student.objects.filter(
            school=school, is_active=True
        ).order_by("last_name", "first_name")
        self.fields["student"].label_from_instance = (
            lambda s: f"{s.first_name} {s.last_name}"
        )
        self.fields["description"].required = False


class PermissionSlipForm(forms.ModelForm):
    """
    Creates or edits a PermissionSlip — an event requiring guardian
    acknowledgement, tracked per student via PermissionSlipResponse.
    School-wide (school_class is null) or class-scoped, same audience
    pattern as AnnouncementForm: `school_class` is optional and, left
    blank, applies to every active student at the school.

    `school` is passed in explicitly by the view (never taken from POST
    data — see core.views.permission_slip_new/permission_slip_edit),
    same pattern as every other school-scoped form here.

    Deliberately doesn't expose PermissionSlipResponse rows here — those
    are generated separately (core.views._sync_permission_slip_responses)
    once the slip itself is saved, since the response rows depend on
    which students are actually eligible (active, with at least one
    linked guardian) rather than being something this form's submitter
    picks directly.
    """

    class Meta:
        model = PermissionSlip
        fields = ["title", "description", "school_class", "event_date", "response_deadline"]
        widgets = {
            "title": forms.TextInput(
                attrs={"class": "input", "placeholder": "e.g. Museum field trip"}
            ),
            "description": forms.Textarea(attrs={"class": "input", "rows": 5}),
            "school_class": forms.Select(attrs={"class": "select"}),
            "event_date": forms.DateInput(attrs={"class": "input", "type": "date"}),
            "response_deadline": forms.DateInput(attrs={"class": "input", "type": "date"}),
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.school = school
        self.fields["school_class"].queryset = SchoolClass.objects.filter(
            school=school, is_active=True
        )
        self.fields["school_class"].required = False
        self.fields["school_class"].empty_label = "School-wide (all students)"
        self.fields["description"].required = False
        self.fields["event_date"].required = False
        self.fields["response_deadline"].required = False
