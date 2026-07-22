from .models import SchoolMembership


class ActiveSchoolMiddleware:
    """
    Resolves the logged-in user's "active school" for this request from
    their SchoolMembership rows and attaches it to the request as
    request.school / request.membership / request.memberships.

    This is the enforcement seam for row-level multi-tenancy (proposal
    Section 5): a view that scopes its querysets by request.school
    structurally cannot leak another school's data, because the active
    school is derived from the authenticated user's own membership
    rows — never from a URL parameter, query string, or POST body a
    client could tamper with.

    A user can hold active memberships at more than one school (a
    teacher whose own child attends the same school, a helper who
    guardians students at two different schools). request.session
    stores which one is "active" under 'active_school_id' so a school
    switcher can change it; if nothing is stored, or the stored school
    no longer matches an active membership, the first one (alphabetical
    by school name) is used and stored back to the session.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.school = None
        request.membership = None
        request.memberships = []

        if request.user.is_authenticated:
            memberships = list(
                SchoolMembership.objects.filter(user=request.user, is_active=True)
                .select_related("school")
                .order_by("school__name")
            )
            request.memberships = memberships

            if memberships:
                active_id = request.session.get("active_school_id")
                membership = next(
                    (m for m in memberships if str(m.school_id) == str(active_id)),
                    None,
                )
                if membership is None:
                    membership = memberships[0]
                    request.session["active_school_id"] = str(membership.school_id)

                request.membership = membership
                request.school = membership.school

        return self.get_response(request)
