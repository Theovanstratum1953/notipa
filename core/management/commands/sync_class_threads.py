"""
Management command: backfill/repair class-group MessageThreads.

core.messaging.sync_class_thread normally runs automatically off signals
(GuardianLink/Student/SchoolClass changes, and School.messaging_enabled
flipping from off to on — see core/signals.py). Those signals only fire
on an actual save() *transition*, so a school whose messaging_enabled
was already True before this feature's code was deployed (or before a
running server process picked up the new code/migrations) never gets
its classes' threads created automatically — there's no "off to on"
event to catch after the fact.

This command re-syncs every class at every messaging-enabled school,
unconditionally, so a stale deployment (or any other reason threads
drifted out of sync) can be fixed without a database edit. Safe to
re-run any time: sync_class_thread is idempotent, and this command
never removes a thread, only creates missing ones and reconciles
participants.

Usage:
    python manage.py sync_class_threads
"""
from django.core.management.base import BaseCommand

from core.messaging import sync_all_class_threads
from core.models import School


class Command(BaseCommand):
    help = (
        "Backfill/repair class-group MessageThreads for every "
        "messaging-enabled school. Safe to re-run."
    )

    def handle(self, *args, **options):
        schools = School.objects.filter(messaging_enabled=True)
        if not schools.exists():
            self.stdout.write("No schools have messaging turned on — nothing to do.")
            return

        for school in schools:
            sync_all_class_threads(school)
            self.stdout.write(f"Synced class threads for “{school.name}”.")

        self.stdout.write(self.style.SUCCESS(f"Done — {schools.count()} school(s) synced."))
