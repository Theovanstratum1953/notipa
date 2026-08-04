from .settings import *  # noqa

STORAGES["staticfiles"]["BACKEND"] = "django.contrib.staticfiles.storage.StaticFilesStorage"

# core.exports.run_export_job normally runs on a background thread
# (dispatched from core.views.export_download) so a large export
# doesn't block the request. Tests need the result immediately and
# deterministically, so under test_settings the same view runs the job
# inline instead — see the "Data export" section of core/views.py.
EXPORT_JOBS_RUN_SYNC = True
