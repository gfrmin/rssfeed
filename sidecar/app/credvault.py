"""Per-domain subscription credentials, stored in gnome-keyring (never the DB).

Credentials are held only so the worker can silently re-login when a paywall
session expires. We shell out to ``secret-tool`` (libsecret) under a dedicated
``service=rssfeed-login`` namespace, keyed by domain — distinct from the global
``service=env`` convention used for machine secrets.

Requires an unlocked keyring reachable over the user D-Bus. The sidecar runs as a
``systemctl --user`` service so it shares ``/run/user/<uid>/bus``; every call is
fail-soft (returns None/False and logs) so a locked or absent keyring degrades to
"no saved credentials" rather than crashing a request or the worker loop.
"""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

_SERVICE = "rssfeed-login"


async def _run(*args: str, stdin: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "secret-tool", *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    return proc.returncode, out.decode().strip(), err.decode().strip()


async def store_credentials(domain: str, username: str, password: str) -> bool:
    """Persist {username, password} for a domain. Returns True on success."""
    payload = json.dumps({"username": username, "password": password})
    try:
        rc, _, err = await _run(
            "store", "--label", f"rssfeed login {domain}",
            "service", _SERVICE, "key", domain,
            stdin=payload,
        )
    except FileNotFoundError:
        logger.warning("secret-tool not found — cannot store credentials for %s", domain)
        return False
    except Exception:
        logger.exception("Failed to store credentials for %s", domain)
        return False
    if rc != 0:
        logger.warning("secret-tool store failed for %s: %s", domain, err)
        return False
    return True


async def get_credentials(domain: str) -> dict | None:
    """Return {username, password} for a domain, or None if absent/unreadable."""
    try:
        rc, out, _ = await _run("lookup", "service", _SERVICE, "key", domain)
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("Failed to look up credentials for %s", domain)
        return None
    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    if not data.get("username") or not data.get("password"):
        return None
    return data


async def has_credentials(domain: str | None) -> bool:
    if not domain:
        return False
    return await get_credentials(domain) is not None


async def delete_credentials(domain: str) -> bool:
    """Remove stored credentials for a domain. Returns True if the call succeeded."""
    try:
        rc, _, _ = await _run("clear", "service", _SERVICE, "key", domain)
    except FileNotFoundError:
        return False
    except Exception:
        logger.exception("Failed to delete credentials for %s", domain)
        return False
    return rc == 0
