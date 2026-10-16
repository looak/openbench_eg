import os

from .settings import *  # noqa: F401,F403


ISOLATED_ROOT = os.environ.get("OPENBENCH_ISOLATED_ROOT")
if not ISOLATED_ROOT:
    raise RuntimeError("OPENBENCH_ISOLATED_ROOT must be set for OpenSite.settings_isolated")

DATABASES["default"]["NAME"] = os.path.join(ISOLATED_ROOT, "db.sqlite3")
MEDIA_ROOT = os.path.join(ISOLATED_ROOT, "Media")
STATIC_ROOT = os.path.join(ISOLATED_ROOT, "staticfiles")
