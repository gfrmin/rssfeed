"""Test bootstrap.

app.config reads DATABASE_URL at import time, so set a dummy before any app
import. These unit tests only exercise pure logic — no connection is opened.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
