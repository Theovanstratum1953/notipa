"""
URL configuration for notipa project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    # Standard Django username/password login/logout — /accounts/login/,
    # /accounts/logout/, etc. Provides the 'login' and 'logout' URL names
    # referenced by LOGIN_URL/LOGOUT_REDIRECT_URL in settings.py.
    path('accounts/', include('django.contrib.auth.urls')),
    path('', include('core.urls', namespace='core')),
]

# Serves uploaded files (e.g. Homework attachments — core.models.
# Homework.attachment, the first real FileField this app uses) from
# MEDIA_ROOT during development. Django never serves user-uploaded
# media itself in production — that's WhiteNoise's job for static
# assets only; a real deployment needs S3-compatible object storage or
# equivalent for MEDIA (see the handover plan's "Not Yet Built" notes),
# which is why this is gated on DEBUG rather than always-on.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
