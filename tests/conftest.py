"""Explicit local-auth defaults for the test process.

Production code intentionally refuses to infer a deployment environment from
missing variables.  Tests that exercise the local mode opt in here instead of
depending on the application's unsafe historical defaults.
"""

import os


os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("AUTH_ENABLED", "false")
