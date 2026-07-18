"""SSRF egress guard: scheme/host sanity, resolved-IP policy, redirect hops.

Hermetic: DNS goes through egress._resolve (stubbed per-test) and HTTP through
an httpx.MockTransport injected via client_kwargs — no network I/O anywhere.
"""
import asyncio

import httpx
import pytest

from app import egress
from app.egress import EgressBlockedError, check_public, check_scheme_host, guarded_get
from app.extractor import _fetch_html, fetch_proxied_image


def _run(coro):
    return asyncio.run(coro)


def _stub_dns(monkeypatch, mapping):
    async def fake_resolve(host, port):
        return mapping[host]
    monkeypatch.setattr(egress, "_resolve", fake_resolve)


# --- check_scheme_host: schemes and hosts -----------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://internal/1",
    "ftp://ftp.example.com/pub",
    "javascript:alert(1)",
    "data:text/html,hi",
    "http://",                                    # no hostname at all
])
def test_bad_scheme_or_missing_host_rejected(url):
    with pytest.raises(EgressBlockedError):
        check_scheme_host(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://10.0.0.8/x",
    "http://172.16.4.2/x",
    "http://192.168.1.10/x",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://100.64.0.7/x",                        # CGNAT / tailnet range
    "http://0.0.0.0/x",
    "http://[::1]/x",
    "http://[fe80::1]/x",
    "http://[fd00::2]/x",
    "http://[::ffff:127.0.0.1]/x",                # v6-mapped v4 loopback
    "http://[::ffff:10.0.0.1]/x",                 # v6-mapped v4 private
])
def test_non_public_ip_literal_rejected(url):
    with pytest.raises(EgressBlockedError):
        check_scheme_host(url)


def test_public_ip_literal_and_hostnames_pass():
    check_scheme_host("http://8.8.8.8/x")
    check_scheme_host("https://example.com/x")    # names defer to check_public


# --- check_public: what the hostname resolves to ----------------------------

def test_hostname_resolving_private_rejected(monkeypatch):
    _stub_dns(monkeypatch, {"internal.example": ["10.1.2.3"]})
    with pytest.raises(EgressBlockedError):
        _run(check_public("http://internal.example/x"))


def test_localhost_names_rejected(monkeypatch):
    _stub_dns(monkeypatch, {"localhost": ["127.0.0.1", "::1"]})
    with pytest.raises(EgressBlockedError):
        _run(check_public("http://localhost:8080/x"))


def test_any_private_answer_rejects_mixed_resolution(monkeypatch):
    _stub_dns(monkeypatch, {"evil.example": ["8.8.8.8", "192.168.0.1"]})
    with pytest.raises(EgressBlockedError):
        _run(check_public("http://evil.example/x"))


def test_public_hostname_passes(monkeypatch):
    _stub_dns(monkeypatch, {"good.example": ["8.8.8.8"]})
    _run(check_public("https://good.example/x"))


def test_unresolvable_hostname_rejected(monkeypatch):
    async def fail(host, port):
        raise OSError("no such host")
    monkeypatch.setattr(egress, "_resolve", fail)
    with pytest.raises(EgressBlockedError):
        _run(check_public("http://nx.example/x"))


# --- guarded_get: every redirect hop re-validated ----------------------------

def test_redirect_to_private_rejected(monkeypatch):
    _stub_dns(monkeypatch, {"pub.example": ["8.8.8.8"]})

    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(EgressBlockedError):
        _run(guarded_get({"transport": httpx.MockTransport(handler)}, "http://pub.example/a"))


def test_public_redirect_chain_followed(monkeypatch):
    _stub_dns(monkeypatch, {"pub.example": ["8.8.8.8"], "pub2.example": ["9.9.9.9"]})

    def handler(request):
        if request.url.host == "pub.example":
            return httpx.Response(301, headers={"location": "http://pub2.example/b"})
        return httpx.Response(200, text="ok")

    r = _run(guarded_get({"transport": httpx.MockTransport(handler)}, "http://pub.example/a"))
    assert r.text == "ok"


def test_redirect_loop_gives_up(monkeypatch):
    _stub_dns(monkeypatch, {"loop.example": ["8.8.8.8"]})

    def handler(request):
        return httpx.Response(302, headers={"location": "http://loop.example/again"})

    with pytest.raises(EgressBlockedError):
        _run(guarded_get({"transport": httpx.MockTransport(handler)}, "http://loop.example/a"))


# --- kill switch --------------------------------------------------------------

def test_kill_switch_disables_checks(monkeypatch):
    monkeypatch.setattr(egress, "EGRESS_GUARD", False)
    check_scheme_host("file:///etc/passwd")
    _run(check_public("http://127.0.0.1/x"))


# --- integration: the extractor entry points ----------------------------------

def test_fetch_html_blocks_bad_scheme():
    assert _run(_fetch_html("file:///etc/passwd")) == (None, None)


def test_fetch_proxied_image_raises_for_loopback():
    with pytest.raises(EgressBlockedError):
        _run(fetch_proxied_image("http://127.0.0.1/a.png"))
