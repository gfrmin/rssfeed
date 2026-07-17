"""Test bootstrap.

app.config reads its environment at import time, so anything the app reads on
import has to be pinned here, before any app import. These unit tests only
exercise pure logic — no connection is opened.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

# Pin login recipes to a path that cannot exist. Without this the suite loads
# whatever the developer happens to have in ~/.config/rssfeed/login-recipes.json —
# so the tests would read real operator config, pass on that machine, and behave
# differently in CI. Tests that care about recipes inject their own via monkeypatch.
os.environ.setdefault("LOGIN_RECIPES_FILE", "/nonexistent/test-login-recipes.json")
