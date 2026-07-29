"""
Class group messaging — the sync logic that keeps a SchoolClass's single
CLASS-type MessageThread (core.models.MessageThread) matching current
reality: created automatically the first time a school's messaging
setting is on for a class with at least one enrolled student, and its
`participants` kept equal to "every guardian linked to a currently
active, enrolled student in the class, plus the class's homeroom and
co-teachers" — no manual invite step for either side (proposal: "Class
Group Messaging" > "Automatic membership").

sync_class_thread is the single entry point; core.signals wires it to
every model change that can affect either side of that membership
(GuardianLink save/delete, Student save, SchoolClass save and its
additional_teachers M2M, and School.messaging_enabled turning on via
sync_all_class_threads). Kept in its own module, not core.views, so
core.signals can import it without importing the whole view layer.
"""
from .models import GuardianLink, MessageThread, SchoolClass, Student


def _guardian_ids_for_class(school_class):
    """User ids of every guardian linked to a currently active student
    in this class — the guardian half of a class thread's participants."""
    return set(
        GuardianLink.objects.filter(
            student__school_class=school_class, student__is_active=True
        ).values_list("guardian_id", flat=True)
    )


def _teacher_ids_for_class(school_class):
    """User ids of the homeroom teacher plus any co-teachers currently
    assigned to this class — the teacher half of a class thread's
    participants. Same set core.views._connected_teacher_ids_for_student
    computes per-student; this is the per-class equivalent."""
    ids = set(school_class.additional_teachers.values_list("user_id", flat=True))
    if school_class.homeroom_teacher_id:
        ids.add(school_class.homeroom_teacher.user_id)
    return ids


def sync_class_thread(school_class):
    """
    Ensures the one CLASS-type MessageThread for `school_class` exists
    (creating it the first time this runs for a class with at least one
    active student, once the school has messaging turned on) and that
    its participants exactly match current enrollment + current teaching
    staff — added the moment a guardian/teacher gains a connection to
    the class, removed the moment they lose it.

    Idempotent and cheap to call speculatively from a signal on a class
    that will never qualify: it's a no-op (no thread created) until both
    preconditions hold (messaging on, at least one active student), and
    it never deletes the thread once created — even if every student
    later leaves the class, the thread and its history stay, only
    participants shrink to (in that case) just the teachers, mirroring
    the "revoke, don't erase" pattern the rest of this app uses for
    memberships/classes/students.

    Returns the thread (existing, newly created, or freshly synced), or
    None if messaging is off for the school or no thread exists yet and
    none is warranted.
    """
    if school_class is None or not school_class.school.messaging_enabled:
        return None

    thread = MessageThread.objects.filter(
        thread_type=MessageThread.ThreadType.CLASS, school_class=school_class
    ).first()

    if thread is None:
        has_student = Student.objects.filter(
            school_class=school_class, is_active=True
        ).exists()
        if not has_student:
            return None
        thread = MessageThread.objects.create(
            school=school_class.school,
            thread_type=MessageThread.ThreadType.CLASS,
            school_class=school_class,
        )

    desired_ids = _guardian_ids_for_class(school_class) | _teacher_ids_for_class(school_class)
    current_ids = set(thread.participants.values_list("id", flat=True))
    to_add = desired_ids - current_ids
    to_remove = current_ids - desired_ids
    if to_add:
        thread.participants.add(*to_add)
    if to_remove:
        thread.participants.remove(*to_remove)
    return thread


def sync_all_class_threads(school):
    """
    Re-syncs every class in a school — called when
    School.messaging_enabled is switched on, since every class in that
    school may newly qualify for a thread at once (rather than only the
    next time each one happens to be touched individually).
    """
    for school_class in SchoolClass.objects.filter(school=school):
        sync_class_thread(school_class)
