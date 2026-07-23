"""
Management command: seed the database with demo data for manual testing
and demos.

Creates:
  - 1 school
  - 6 classes (one per grade, Grade 1 through Grade 6)
  - 6 teachers, one set as homeroom teacher of each class
  - 20-30 students per class (random count within that range)
  - 1 school admin
  - 1 guardian with 3 children enrolled in 3 different classes

Usage:
    python manage.py seed_demo
    python manage.py seed_demo --reset          # wipe demo data first, then recreate
    python manage.py seed_demo --seed 42        # reproducible random data

Safe to re-run: records are looked up with get_or_create/exclusion checks
keyed on the demo school name and the @demo.notipa.local email domain, so
running the command twice does not create duplicate schools, classes, or
users. New students are only added if a class currently has fewer than
its randomly chosen target headcount.
"""
import random
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import (
    GuardianLink,
    School,
    SchoolClass,
    SchoolMembership,
    Student,
)

User = get_user_model()

DEMO_EMAIL_DOMAIN = "demo.notipa.local"
DEMO_PASSWORD = "DemoPass123!"

SCHOOL_NAME = "Sunflower Elementary School (Demo)"

GRADE_NAMES = [
    "Grade 1 - Acacia",
    "Grade 2 - Bougainvillea",
    "Grade 3 - Camia",
    "Grade 4 - Dahlia",
    "Grade 5 - Everlasting",
    "Grade 6 - Firethorn",
]

TEACHER_NAMES = [
    ("Maria", "Santos"),
    ("Jose", "Reyes"),
    ("Ana", "Cruz"),
    ("Ramon", "Bautista"),
    ("Carmela", "Aquino"),
    ("Ferdinand", "Garcia"),
]

ADMIN_NAME = ("Liza", "Mendoza")
GUARDIAN_NAME = ("Rosario", "Villanueva")

STUDENT_FIRST_NAMES = [
    "Juan", "Maria", "Jose", "Andrea", "Miguel", "Sofia", "Gabriel", "Isabella",
    "Rafael", "Camille", "Diego", "Angelica", "Mateo", "Bianca", "Antonio",
    "Krista", "Emmanuel", "Nicole", "Vincent", "Patricia", "Marco", "Kristine",
    "Joaquin", "Alyssa", "Adrian", "Samantha", "Julian", "Erika", "Sebastian",
    "Faith", "Xavier", "Hannah", "Nathaniel", "Janella", "Elijah", "Trisha",
]

STUDENT_LAST_NAMES = [
    "Dela Cruz", "Santos", "Reyes", "Cruz", "Bautista", "Ocampo", "Garcia",
    "Mendoza", "Torres", "Flores", "Ramos", "Villanueva", "Gonzales", "Aquino",
    "Castillo", "Del Rosario", "Navarro", "Pascual", "Domingo", "Salazar",
]

STUDENTS_PER_CLASS_RANGE = (20, 30)


class Command(BaseCommand):
    help = (
        "Seed the database with a demo school: 1 school, 6 classes, 6 "
        "teachers (one homeroom teacher per class), 20-30 students per "
        "class, 1 school admin, and 1 guardian with 3 children in "
        "different classes. Safe to re-run. Pass --reset to wipe and "
        "recreate the demo data instead of reusing it."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Delete any existing demo data (matched by the demo school "
                f"name and @{DEMO_EMAIL_DOMAIN} user accounts) before seeding."
            ),
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=None,
            help="Optional random seed, for reproducible demo data.",
        )

    def handle(self, *args, **options):
        if options["seed"] is not None:
            random.seed(options["seed"])

        if options["reset"]:
            self._reset()

        with transaction.atomic():
            school = self._create_school()
            teacher_memberships = self._create_teachers(school)
            classes = self._create_classes(school, teacher_memberships)
            self._create_students(classes)
            admin_user = self._create_admin(school)
            guardian_user, children = self._create_guardian_with_children(school, classes)

        self.stdout.write(self.style.SUCCESS("\nDemo data ready."))
        self.stdout.write(f"  School:   {school.name}  (id={school.id})")
        self.stdout.write(f"  Admin:    {admin_user.email} / {DEMO_PASSWORD}")
        self.stdout.write("  Teachers:")
        for membership, school_class in zip(teacher_memberships, classes):
            self.stdout.write(
                f"    {membership.user.email} / {DEMO_PASSWORD}  -> homeroom of {school_class.name}"
            )
        self.stdout.write(f"  Guardian: {guardian_user.email} / {DEMO_PASSWORD}")
        self.stdout.write("  Guardian's children:")
        for child in children:
            self.stdout.write(
                f"    {child.first_name} {child.last_name} — {child.school_class.name}"
            )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------
    def _reset(self):
        self.stdout.write("Resetting existing demo data...")
        School.objects.filter(name=SCHOOL_NAME).delete()
        User.objects.filter(email__iendswith=f"@{DEMO_EMAIL_DOMAIN}").delete()

    # ------------------------------------------------------------------
    # School
    # ------------------------------------------------------------------
    def _create_school(self):
        school, created = School.objects.get_or_create(
            name=SCHOOL_NAME,
            defaults={
                "country": "PH",
                "default_language": "en",
                "timezone": "Asia/Manila",
                "tier": School.Tier.FREE,
                "academic_year_start_month": 6,
                "is_active": True,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Reusing'} school: {school.name}")
        )
        return school

    def _current_academic_year(self, school):
        today = date.today()
        if today.month >= school.academic_year_start_month:
            start_year = today.year
        else:
            start_year = today.year - 1
        return f"{start_year}-{start_year + 1}"

    # ------------------------------------------------------------------
    # Users (shared helper)
    # ------------------------------------------------------------------
    def _get_or_create_user(self, *, slug, first_name, last_name, phone_suffix):
        email = f"{slug}@{DEMO_EMAIL_DOMAIN}"
        user, created = User.objects.get_or_create(
            username=email,
            defaults={
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "phone_number": f"+63917000{phone_suffix:04d}",
                "preferred_language": "en",
            },
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save(update_fields=["password"])
        return user, created

    # ------------------------------------------------------------------
    # Teachers
    # ------------------------------------------------------------------
    def _create_teachers(self, school):
        memberships = []
        for i, (first_name, last_name) in enumerate(TEACHER_NAMES, start=1):
            slug = f"teacher{i}"
            user, created = self._get_or_create_user(
                slug=slug, first_name=first_name, last_name=last_name, phone_suffix=i,
            )
            membership, _ = SchoolMembership.objects.get_or_create(
                user=user,
                school=school,
                role=SchoolMembership.Role.TEACHER,
                defaults={"is_active": True},
            )
            memberships.append(membership)
            self.stdout.write(
                self.style.SUCCESS(f"{'Created' if created else 'Reusing'} teacher: {user.email}")
            )
        return memberships

    # ------------------------------------------------------------------
    # Classes
    # ------------------------------------------------------------------
    def _create_classes(self, school, teacher_memberships):
        academic_year = self._current_academic_year(school)
        classes = []
        for name, homeroom in zip(GRADE_NAMES, teacher_memberships):
            school_class, created = SchoolClass.objects.get_or_create(
                school=school,
                name=name,
                academic_year=academic_year,
                defaults={"homeroom_teacher": homeroom, "is_active": True},
            )
            if not created and school_class.homeroom_teacher_id != homeroom.id:
                school_class.homeroom_teacher = homeroom
                school_class.save(update_fields=["homeroom_teacher"])
            classes.append(school_class)
            self.stdout.write(
                self.style.SUCCESS(f"{'Created' if created else 'Reusing'} class: {school_class.name}")
            )
        return classes

    # ------------------------------------------------------------------
    # Students
    # ------------------------------------------------------------------
    def _create_students(self, classes):
        student_counter = 1
        for school_class in classes:
            existing = school_class.students.count()
            target = random.randint(*STUDENTS_PER_CLASS_RANGE)
            to_create = max(0, target - existing)
            for _ in range(to_create):
                first_name = random.choice(STUDENT_FIRST_NAMES)
                last_name = random.choice(STUDENT_LAST_NAMES)
                Student.objects.create(
                    school=school_class.school,
                    school_class=school_class,
                    first_name=first_name,
                    last_name=last_name,
                    student_id=f"DEMO-{school_class.name[:7].strip()}-{student_counter:04d}",
                    is_active=True,
                )
                student_counter += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Class '{school_class.name}' now has {school_class.students.count()} students"
                )
            )

    # ------------------------------------------------------------------
    # Admin
    # ------------------------------------------------------------------
    def _create_admin(self, school):
        first_name, last_name = ADMIN_NAME
        user, created = self._get_or_create_user(
            slug="admin", first_name=first_name, last_name=last_name, phone_suffix=900,
        )
        SchoolMembership.objects.get_or_create(
            user=user,
            school=school,
            role=SchoolMembership.Role.ADMIN,
            defaults={"is_active": True},
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Reusing'} school admin: {user.email}")
        )
        return user

    # ------------------------------------------------------------------
    # Guardian with 3 children in different classes
    # ------------------------------------------------------------------
    def _create_guardian_with_children(self, school, classes):
        first_name, last_name = GUARDIAN_NAME
        user, created = self._get_or_create_user(
            slug="guardian1", first_name=first_name, last_name=last_name, phone_suffix=901,
        )
        SchoolMembership.objects.get_or_create(
            user=user,
            school=school,
            role=SchoolMembership.Role.GUARDIAN,
            defaults={"is_active": True},
        )
        self.stdout.write(
            self.style.SUCCESS(f"{'Created' if created else 'Reusing'} guardian: {user.email}")
        )

        existing_links = list(
            GuardianLink.objects.filter(guardian=user).select_related("student__school_class")
        )
        children = [link.student for link in existing_links]
        still_needed = 3 - len(children)
        if still_needed <= 0:
            return user, children[:3]

        already_linked_class_ids = {
            link.student.school_class_id for link in existing_links
        }
        candidate_classes = [c for c in classes if c.id not in already_linked_class_ids]
        chosen_classes = random.sample(candidate_classes, min(still_needed, len(candidate_classes)))

        for school_class in chosen_classes:
            student = (
                school_class.students.exclude(guardian_links__guardian=user)
                .order_by("?")
                .first()
            )
            if student is None:
                continue
            GuardianLink.objects.create(
                guardian=user,
                student=student,
                relationship=GuardianLink.Relationship.MOTHER,
                is_primary_contact=True,
            )
            children.append(student)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Linked child {student.first_name} {student.last_name} in "
                    f"{school_class.name} to guardian {user.email}"
                )
            )
        return user, children
