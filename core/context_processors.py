from django.conf import settings


def vapid_public_key(request):
    """
    Exposes settings.VAPID_PUBLIC_KEY to every template as
    vapid_public_key, so base.html can hand it to the browser's
    PushManager.subscribe() call (core/static/core/js/push.js) without a
    separate request. Only the public key ever reaches a template or the
    client — VAPID_PRIVATE_KEY is used exclusively server-side, in
    core.push, and is never added to any template context.
    """
    return {"vapid_public_key": settings.VAPID_PUBLIC_KEY}
