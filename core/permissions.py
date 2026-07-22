"""
Queryset-level and view-level permission enforcement, built on top of
request.school / request.membership (see core.middleware.
ActiveSchoolMiddleware). This is the piece the handover notes flagged as
"the part not to rush" — a permission-boundary bug on children's data is
a serious failure, not a minor bug (proposal Section 11).

Three things live here:

- role_required(*roles): a view decorator that checks the requesting
  user's *active* school membership has one of the given roles.
- superuser_required: a view decorator for platform-operator actions
  that sit above any school-scoped role — right now, just creating a
  new School (the tenant itself).
- scope_to_school(queryset, request): a helper that filters any
  tenant-scoped queryset down to request.school, with a clear failure
  mode (an empty queryset) rather than an exception if there's no
  active school — since "no school" is a normal state for a brand new
  user, not a bug.
"""
from functools import wraps

from django.shortcuts import render


def role_required(*roles):
    """
    Restrict a view to users whose *active* school membership has one
    of the given roles (core.models.SchoolMembership.Role values).

    Must be used together with @login_required (applied outermost, so
    an anonymous user is sent to the login page rather than shown a
    403), since this decorator assumes request.user is authenticated
    and request.membership has already been set by
    ActiveSchoolMiddleware.

    Usage:
        from django.contrib.auth.decorators import login_required
        from core.models import SchoolMembership
        from core.permissions import role_required

        @login_required
        @role_required(SchoolMembership.Role.ADMIN, SchoolMembership.Role.TEACHER)
        def some_view(request):
            ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.membership is None or request.membership.role not in roles:
                return render(request, "core/no_access.html", status=403)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def superuser_required(view_func):
    """
    Restricts a view to Django superusers. Used for actions that create
    a tenant rather than operate within one — e.g. setting up a new
    School from inside the app (core.views.school_setup) instead of via
    /admin/. Regular school admins manage things within their own
    school; creating a new school is a platform-operator action, one
    level above any SchoolMembership role.
    """

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return render(request, "core/no_access.html", status=403)
        return view_func(request, *args, **kwargs)

    return wrapped


def scope_to_school(queryset, request, field_name="school"):
    """
    Filter a tenant-scoped queryset down to the request's active school.
    Returns queryset.none() if the request has no active school, rather
    than raising — a freshly created user with no SchoolMembership yet
    is an expected state, not an error condition.

    field_name lets this be reused for models where the school
    relationship isn't a direct 'school' FK (e.g. filtering
    PermissionSlipResponse via 'permission_slip__school').
    """
    if request.school is None:
        return queryset.none()
    return queryset.filter(**{field_name: request.school})
