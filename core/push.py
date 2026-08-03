"""
Web Push sending — the server-side half of the last Phase 1 build-sequence
item (proposal: "PWA install + web push notifications"). The client-side
half (subscribing, the service worker's push/notificationclick handlers) is
core/static/core/js/push.js and root_static/sw.js; core.views.push_subscribe/
push_unsubscribe is what connects the two by saving/removing a
core.models.PushSubscription.

Every "new content a guardian (or, for messaging, a teacher) should hear
about" trigger in core.views calls notify_users() from here after saving —
never before, so a push failure can never prevent the actual content from
being created. Sending is deliberately best-effort and silent: a school
running without VAPID keys configured (see notipa/settings.py's own
docstring on this) simply doesn't send pushes, and an individual dead
subscription is pruned rather than raised as an error partway through
saving an announcement/homework item/etc.
"""
import json
import logging

from django.conf import settings

from .models import PushSubscription

logger = logging.getLogger(__name__)

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover — pywebpush is a hard requirement
    # in requirements.txt; this only guards a dev environment that hasn't
    # run `pip install` yet from crashing on import elsewhere in the app.
    webpush = None
    WebPushException = Exception


def push_configured():
    """Whether this server has a VAPID key pair set up at all — see the
    settings.py docstring on VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY for why
    this is optional rather than assumed."""
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY and webpush)


def send_push_notification(subscription, title, body, url=None):
    """
    Sends one Web Push message to one core.models.PushSubscription.
    Returns True if it was sent, False if it was skipped or failed.

    A 404/410 response from the push service means that subscription is
    gone for good (uninstalled, permissions cleared, endpoint expired) —
    those get deleted here so nothing keeps retrying a dead endpoint on
    every future notification. Any other failure (network blip, a
    misconfigured key) is logged and swallowed rather than raised, since
    a push failure is never supposed to be able to break whatever
    request triggered it (e.g. publishing an announcement).
    """
    if not push_configured():
        return False

    payload = {"title": title, "body": body, "url": url or "/"}
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth_key},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{settings.VAPID_ADMIN_EMAIL}"},
        )
        return True
    except WebPushException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in (404, 410):
            subscription.delete()
        else:
            logger.warning("Push send failed (status=%s): %s", status_code, exc)
        return False
    except Exception:  # pragma: no cover — defensive: never let a push
        # failure bubble up into the view that triggered it.
        logger.exception("Unexpected error sending push notification")
        return False


def notify_users(users, title, body, url=None):
    """
    Sends a push notification to every subscription belonging to any
    user in `users` (an iterable of User instances or a queryset).
    Deduplicates users first (the same guardian can be reachable through
    more than one path — e.g. two overlapping recipient sets — and
    should only ever get one notification per event, not one per path
    that happened to include them), then fans out to each of their
    devices independently, since a user may have more than one
    subscription (phone + laptop).

    A no-op, quickly, if push isn't configured at all — see
    push_configured — so every call site can call this unconditionally
    rather than checking first.
    """
    if not push_configured():
        return

    user_ids = {u.id if hasattr(u, "id") else u for u in users}
    user_ids.discard(None)
    if not user_ids:
        return

    subscriptions = PushSubscription.objects.filter(user_id__in=user_ids)
    for subscription in subscriptions:
        send_push_notification(subscription, title, body, url=url)
