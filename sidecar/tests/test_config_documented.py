"""Every setting the app reads must appear in docs/configuration.md.

Settings are cheap to add and easy to leave undocumented — 17 of them were, until
this test existed, discoverable only by reading config.py. This closes that by
construction rather than by discipline: adding an `os.environ` read to config.py
fails the suite until the reference documents it.

config.py is parsed with `ast`, never imported, so this holds even for settings
read at import time under names the module doesn't keep.
"""
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PY = REPO_ROOT / "sidecar" / "app" / "config.py"
CONFIG_DOC = REPO_ROOT / "docs" / "configuration.md"

# Settings below this heading configure the deployment (compose, the unit,
# run-sidecar.sh) rather than the program, so they are documented but must NOT
# appear in config.py.
DEPLOYMENT_HEADING = "## Not read by the app"


def _env_names_in_config() -> set[str]:
    tree = ast.parse(CONFIG_PY.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        # os.environ.get("NAME", ...)
        if isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "get"
                and isinstance(fn.value, ast.Attribute)
                and fn.value.attr == "environ"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                names.add(node.args[0].value)
        # os.environ["NAME"]
        if isinstance(node, ast.Subscript):
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
            ):
                names.add(node.slice.value)
    return names


def _documented_names() -> tuple[set[str], set[str]]:
    """(app settings, deployment settings) as documented, by table row."""
    text = CONFIG_DOC.read_text()
    app_text, _, deploy_text = text.partition(DEPLOYMENT_HEADING)
    row = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \|", re.MULTILINE)
    return set(row.findall(app_text)), set(row.findall(deploy_text))


def test_every_setting_is_documented():
    documented, _ = _documented_names()
    missing = _env_names_in_config() - documented
    assert not missing, (
        f"undocumented in docs/configuration.md: {sorted(missing)} — "
        "add a table row, or move the read out of config.py"
    )


def test_no_stale_documented_settings():
    documented, _ = _documented_names()
    stale = documented - _env_names_in_config()
    assert not stale, (
        f"documented but no longer read by config.py: {sorted(stale)} — "
        f"delete the row, or move it under '{DEPLOYMENT_HEADING}'"
    )


def test_deployment_settings_are_not_app_settings():
    _, deployment = _documented_names()
    assert deployment, "the deployment section lost its table"
    overlap = deployment & _env_names_in_config()
    assert not overlap, (
        f"{sorted(overlap)} is read by config.py, so it is an app setting — "
        f"move it out of '{DEPLOYMENT_HEADING}'"
    )
