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

Also home to the "is messaging actually usable right now" helpers
(proposal: "Per-School Messaging On/Off Switch"): class_messaging_
effectively_enabled and thread_messaging_enabled. Both live here rather
than on the models themselves so core.views, core.forms, and this
module's own sync logic all agree on one definition of "on" — a school
switch and a class switch, both true, never checked separately or
inconsistently at different call sites.
"""
from .models import GuardianLink, MessageThread, SchoolClass, Student


def class_messaging_effectively_enabled(school_class):
    """
    Whether messaging is actually usable for this class right now — both
    the school-wide switch and this class's own opt-out have to be on.
    This is the "no partial state" rule from the proposal: a class's own
    messaging_enabled is meaningless once the school switch is off, so
    every call site should use this rather than checking
    school_class.messaging_enabled on its own.

    Governs both this class's own group thread and any guardian-teacher
    one-to-one thread about one of its students — see
    thread_messaging_enabled, which is the per-thread version of this
    same check.
    """
    return bool(
        school_class
        and school_class.school.messaging_enabled
        and school_class.messaging_enabled
    )


def thread_messaging_enabled(thread):
    """
    Whether *new* messages can currently be posted into this thread — the
    school switch, and (if the thread has a connected class) that
    class's own switch, both have to be on.

    Deliberately does not decide whether the thread can be *viewed* —
    turning either switch off doesn't hide or delete an existing thread
    (proposal: "Clean disable, not just hidden... existing threads are
    preserved, not deleted, but become read-only"); this only backs the
    composer/moderation-visible decisions in core.views.
    message_thread_detail, not access to the thread page itself.
    """
    if not thread.school.messaging_enabled:
        return False
    if thread.thread_type == MessageThread.ThreadType.CLASS:
        relevant_class = thread.school_class
    else:
        relevant_class = thread.student.school_class if thread.student_id else None
    if relevant_class is not None and not relevant_class.messaging_enabled:
        return False
    return True


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
    (creating it the first time this runs for a class that's effectively
    enabled — see class_messaging_effectively_enabled — with at least
    one active student) and that its participants exactly match current
    enrollment + current teaching staff — added the moment a
    guardian/teacher gains a connection to the class, removed the moment
    they lose it.

    Participant syncing only runs at all while the *school* switch is
    on — if a school turns messaging off, this is a no-op even for a
    class whose thread already exists, so an existing thread's
    participant list simply stops changing until the school switch (and
    then core.signals' backfill via sync_all_class_threads) brings it
    current again. The *class*-level switch, by contrast, only gates
    whether a **new** thread gets created in the first place: once a
    class's thread exists, sync_class_thread keeps its participants
    accurate regardless of whether that one class later opts out — only
    core.messaging.thread_messaging_enabled (checked at the view layer)
    decides whether it can still be posted into. That split is what
    makes "read-only, not deleted" from the proposal actually hold:
    turning a class off doesn't touch its thread's membership or
    history at all, only new posting.

    Idempotent and cheap to call speculatively from a signal on a class
    that will never qualify: it's a no-op (no thread created) until
    every precondition holds, and it never deletes the thread once
    created — even if every student later leaves the class, the thread
    and its history stay, only participants shrink to (in that case)
    just the teachers, mirroring the "revoke, don't erase" pattern the
    rest of this app uses for memberships/classes/students.

    Returns the thread (existing, newly created, or freshly synced), or
    None if messaging is off for the school, or no thread exists yet and
    none is warranted.
    """
    if school_class is None or not school_class.school.messaging_enabled:
        return None

    thread = MessageThread.objects.filter(
        thread_type=MessageThread.ThreadType.CLASS, school_class=school_class
    ).first()

    if thread is None:
        if not class_messaging_effectively_enabled(school_class):
            return None
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
    next time each one happens to be touched individually). Also the
    catch-up mechanism for a class whose own messaging_enabled was
    turned on while the school switch was already on (core.signals wires
    SchoolClass's own post_save to sync_class_thread for that specific
    case), or for repairing drift generally — see the
    core.management.commands.sync_class_threads command, which just
    calls this for every messaging-enabled school.
    """
    for school_class in SchoolClass.objects.filter(school=school):
        sync_class_thread(school_class)
