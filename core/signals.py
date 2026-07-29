"""
Wires core.messaging.sync_class_thread/sync_all_class_threads to every
model change that can affect a class thread's membership, so "no manual
invite step for either side" (proposal: "Class Group Messaging" >
"Automatic membership") actually holds without every view that touches
enrollment/teaching-staff having to remember to call the sync helper
itself.

Registered from core.apps.CoreConfig.ready() rather than imported at
module load time — the standard Django pattern, since signal handlers
need the app registry fully populated first.

Each handler is deliberately thin: it figures out which SchoolClass
(sometimes two, on a change) might be affected, then hands off to
core.messaging for the actual sync/creation logic. None of this raises
on a class whose school doesn't have messaging on, or on a class with no
students yet — sync_class_thread itself is the no-op-until-eligible
guard (see its docstring).
"""
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver

from .messaging import sync_all_class_threads, sync_class_thread
from .models import GuardianLink, School, SchoolClass, Student


@receiver(pre_save, sender=Student)
def _student_pre_save(sender, instance, **kwargs):
    """Snapshots the student's current school_class before save, so the
    post_save handler below can also re-sync the class a student just
    moved *out of* — Student itself doesn't retain the old value once
    save() has run."""
    if instance.pk:
        old_class_id = (
            Student.objects.filter(pk=instance.pk).values_list("school_class_id", flat=True).first()
        )
    else:
        old_class_id = None
    instance._old_school_class_id = old_class_id


@receiver(post_save, sender=Student)
def _student_post_save(sender, instance, created, **kwargs):
    old_class_id = getattr(instance, "_old_school_class_id", None)
    if old_class_id and old_class_id != instance.school_class_id:
        old_class = SchoolClass.objects.filter(pk=old_class_id).first()
        if old_class:
            sync_class_thread(old_class)
    if instance.school_class_id:
        sync_class_thread(instance.school_class)


@receiver(post_save, sender=GuardianLink)
def _guardian_link_post_save(sender, instance, **kwargs):
    if instance.student.school_class_id:
        sync_class_thread(instance.student.school_class)


@receiver(post_delete, sender=GuardianLink)
def _guardian_link_post_delete(sender, instance, **kwargs):
    if instance.student.school_class_id:
        sync_class_thread(instance.student.school_class)


@receiver(post_save, sender=SchoolClass)
def _school_class_post_save(sender, instance, **kwargs):
    """Covers both a brand new class (no thread to sync yet, harmless)
    and a change to homeroom_teacher on an existing one."""
    sync_class_thread(instance)


@receiver(m2m_changed, sender=SchoolClass.additional_teachers.through)
def _additional_teachers_changed(sender, instance, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear"):
        sync_class_thread(instance)


@receiver(pre_save, sender=School)
def _school_pre_save(sender, instance, **kwargs):
    """Snapshots messaging_enabled before save, so the post_save handler
    can tell "just turned on" apart from "already on" — sync_all_class_
    threads only needs running on the former."""
    if instance.pk:
        old_value = (
            School.objects.filter(pk=instance.pk).values_list("messaging_enabled", flat=True).first()
        )
    else:
        old_value = None
    instance._old_messaging_enabled = old_value


@receiver(post_save, sender=School)
def _school_post_save(sender, instance, **kwargs):
    old_value = getattr(instance, "_old_messaging_enabled", None)
    if instance.messaging_enabled and not old_value:
        sync_all_class_threads(instance)
