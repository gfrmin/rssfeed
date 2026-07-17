"""Loading per-site login recipes from operator config.

Recipes moved out of the source tree because the set of sites you can log into is
the set of subscriptions you hold — configuration, not code. That makes the load
path a real dependency: it must degrade to "no recipes" (the generic heuristic
login still works) rather than break the app, whatever it finds on disk.
"""
import json

from app import browser_login


def _write(tmp_path, obj):
    p = tmp_path / "login-recipes.json"
    p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")
    return str(p)


def test_missing_file_yields_no_recipes():
    """The default for anyone who never configures one — must not raise."""
    assert browser_login.load_recipes("/nonexistent/login-recipes.json") == {}


def test_empty_path_yields_no_recipes():
    assert browser_login.load_recipes("") == {}


def test_loads_a_recipe(tmp_path):
    path = _write(tmp_path, {
        "paywall.example.com": {
            "login_url": "https://paywall.example.com/login/",
            "username_selectors": ["#login-email"],
            "password_selectors": ["#login-password"],
            "submit_selectors": ["button.login"],
        }
    })
    got = browser_login.load_recipes(path)
    assert set(got) == {"paywall.example.com"}
    r = got["paywall.example.com"]
    assert r.login_url == "https://paywall.example.com/login/"
    assert r.username_selectors == ["#login-email"]
    assert r.submit_selectors == ["button.login"]


def test_selectors_are_optional(tmp_path):
    """A recipe may exist only to pin the login URL; selectors then fall back to
    the built-in heuristics."""
    path = _write(tmp_path, {"a.example.com": {"login_url": "https://a.example.com/in"}})
    r = browser_login.load_recipes(path)["a.example.com"]
    assert r.username_selectors == [] and r.password_selectors == []


def test_domain_keys_are_lowercased(tmp_path):
    path = _write(tmp_path, {"A.Example.COM": {"login_url": "https://a.example.com/in"}})
    assert "a.example.com" in browser_login.load_recipes(path)


def test_comment_keys_are_ignored(tmp_path):
    """JSON has no comments, so the example file documents itself with a __key."""
    path = _write(tmp_path, {
        "__comment": ["docs", "here"],
        "a.example.com": {"login_url": "https://a.example.com/in"},
    })
    assert set(browser_login.load_recipes(path)) == {"a.example.com"}


def test_one_bad_recipe_does_not_kill_the_others(tmp_path):
    """A typo in one entry shouldn't silently disable every other site's login."""
    path = _write(tmp_path, {
        "bad.example.com": {"no_login_url": "oops"},
        "worse.example.com": "not even an object",
        "good.example.com": {"login_url": "https://good.example.com/in"},
    })
    assert set(browser_login.load_recipes(path)) == {"good.example.com"}


def test_malformed_json_yields_no_recipes(tmp_path):
    path = _write(tmp_path, "{ this is not json")
    assert browser_login.load_recipes(path) == {}


def test_non_object_top_level_yields_no_recipes(tmp_path):
    path = _write(tmp_path, ["a", "list"])
    assert browser_login.load_recipes(path) == {}


def test_warning_never_echoes_the_recipe_body(tmp_path, caplog):
    """Log lines name the domain only — a recipe body carries a login URL, and
    logs are the classic way private config leaks back out."""
    path = _write(tmp_path, {"bad.example.com": {"secret_url": "https://private.example.com/x"}})
    with caplog.at_level("WARNING"):
        browser_login.load_recipes(path)
    assert "bad.example.com" in caplog.text
    assert "private.example.com" not in caplog.text


# --- the public API still behaves with no recipes configured ----------------

def test_has_login_recipe_false_without_config(monkeypatch):
    monkeypatch.setattr(browser_login, "LOGIN_RECIPES", {})
    assert browser_login.has_login_recipe("anything.example.com") is False
    assert browser_login.has_login_recipe(None) is False


def test_recipe_for_falls_back_to_heuristic_url(monkeypatch):
    monkeypatch.setattr(browser_login, "LOGIN_RECIPES", {})
    r = browser_login.recipe_for("example.com")
    assert r.login_url == "https://example.com/login"
    assert r.username_selectors == []


def test_recipe_matches_with_and_without_www(monkeypatch):
    monkeypatch.setattr(browser_login, "LOGIN_RECIPES", {
        "example.com": browser_login.LoginRecipe(login_url="https://example.com/in")
    })
    assert browser_login.has_login_recipe("www.example.com") is True
    assert browser_login.recipe_for("www.example.com").login_url == "https://example.com/in"
