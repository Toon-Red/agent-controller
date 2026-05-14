"""AC-S16e tests for the Discord async ping integration."""
from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import discord_ping


# ---------------------------------------------------------------------------
# Fake opener for urllib.request.urlopen
# ---------------------------------------------------------------------------

class _OK:
    def __init__(self, body: bytes = b'{"ok": true}'):
        self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body


def _ok_opener(req, timeout=None):
    return _OK()


def _http_error(code: int):
    def _open(req, timeout=None):
        raise urllib.error.HTTPError("http://x", code, "fail", {}, BytesIO(b""))
    return _open


@pytest.fixture(autouse=True)
def _reset_dedup():
    discord_ping.reset_dedup_cache()
    yield
    discord_ping.reset_dedup_cache()


# ---------------------------------------------------------------------------
# Payload + level mapping
# ---------------------------------------------------------------------------

class TestEmbedPayload:
    def test_info_payload_shape(self):
        p = discord_ping.build_embed_payload("hello", "body text", "info")
        assert p["embeds"][0]["title"] == "[INFO] hello"
        assert p["embeds"][0]["description"] == "body text"
        assert p["embeds"][0]["color"] == 0x3498DB

    def test_warning_color(self):
        p = discord_ping.build_embed_payload("h", "b", "warning")
        assert p["embeds"][0]["color"] == 0xF1C40F
        assert p["embeds"][0]["title"].startswith("[WARN]")

    def test_critical_color(self):
        p = discord_ping.build_embed_payload("h", "b", "critical")
        assert p["embeds"][0]["color"] == 0xE74C3C
        assert p["embeds"][0]["title"].startswith("[CRIT]")

    def test_unknown_level_rejected(self):
        with pytest.raises(ValueError, match="unknown level"):
            discord_ping.build_embed_payload("h", "b", "fatal")  # type: ignore

    def test_extra_fields_attached(self):
        p = discord_ping.build_embed_payload(
            "h", "b", "info",
            extra_fields=[{"name": "x", "value": "y"}],
        )
        assert p["embeds"][0]["fields"] == [{"name": "x", "value": "y"}]


# ---------------------------------------------------------------------------
# send_escalation: happy path with mocked webhook
# ---------------------------------------------------------------------------

class TestSendEscalation:
    def test_happy_path_returns_true(self, monkeypatch):
        captured = []
        def fake_opener(req, timeout=None):
            captured.append(req.data)
            return _OK()
        ok = discord_ping.send_escalation(
            "test title", "body", "info",
            webhook_url="http://example/webhook",
            opener=fake_opener,
        )
        assert ok is True
        payload = json.loads(captured[0])
        assert payload["embeds"][0]["title"] == "[INFO] test title"

    def test_missing_webhook_env_no_op(self, monkeypatch):
        monkeypatch.delenv(discord_ping.ENV_WEBHOOK, raising=False)
        ok = discord_ping.send_escalation("t", "b", "info")
        assert ok is False  # graceful degradation, no crash

    def test_uses_env_webhook_when_url_not_passed(self, monkeypatch):
        monkeypatch.setenv(discord_ping.ENV_WEBHOOK, "http://from-env/")
        captured = []
        def fake_opener(req, timeout=None):
            captured.append(req.full_url)
            return _OK()
        ok = discord_ping.send_escalation("t", "b", "info", opener=fake_opener)
        assert ok is True
        assert captured[0] == "http://from-env/"

    def test_http_error_returns_false_no_raise(self):
        ok = discord_ping.send_escalation(
            "t", "b", "info",
            webhook_url="http://x", opener=_http_error(429),
        )
        assert ok is False  # rate-limited; degrades gracefully

    def test_network_error_returns_false(self):
        def boom(req, timeout=None):
            raise ConnectionError("net down")
        ok = discord_ping.send_escalation(
            "t", "b", "info",
            webhook_url="http://x", opener=boom,
        )
        assert ok is False

    def test_invalid_level_returns_false(self):
        ok = discord_ping.send_escalation(
            "t", "b", "fatal",  # type: ignore
            webhook_url="http://x", opener=_ok_opener,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class TestDedup:
    def test_same_title_body_skipped_within_window(self):
        n_calls = {"n": 0}
        def fake_opener(req, timeout=None):
            n_calls["n"] += 1
            return _OK()
        # First call succeeds.
        ok1 = discord_ping.send_escalation(
            "Decision needed", "Choose A or B", "warning",
            webhook_url="http://x", opener=fake_opener,
        )
        # Second call with same title+body should dedup-skip.
        ok2 = discord_ping.send_escalation(
            "Decision needed", "Choose A or B", "warning",
            webhook_url="http://x", opener=fake_opener,
        )
        assert ok1 is True
        assert ok2 is False
        assert n_calls["n"] == 1  # only the first one hit the webhook

    def test_different_body_not_deduped(self):
        n_calls = {"n": 0}
        def fake_opener(req, timeout=None):
            n_calls["n"] += 1
            return _OK()
        discord_ping.send_escalation(
            "Decision needed", "Choose A", "warning",
            webhook_url="http://x", opener=fake_opener,
        )
        discord_ping.send_escalation(
            "Decision needed", "Choose B", "warning",
            webhook_url="http://x", opener=fake_opener,
        )
        assert n_calls["n"] == 2

    def test_dedup_false_disables_skip(self):
        n_calls = {"n": 0}
        def fake_opener(req, timeout=None):
            n_calls["n"] += 1
            return _OK()
        for _ in range(3):
            discord_ping.send_escalation(
                "x", "y", "info",
                webhook_url="http://x", opener=fake_opener,
                dedup=False,
            )
        assert n_calls["n"] == 3


# ---------------------------------------------------------------------------
# DiscordPingSurface (DiscordSurface protocol impl)
# ---------------------------------------------------------------------------

class TestDiscordPingSurface:
    def test_post_invokes_send_escalation(self, monkeypatch):
        captured = []
        def fake_opener(req, timeout=None):
            captured.append(json.loads(req.data))
            return _OK()
        surface = discord_ping.DiscordPingSurface(
            webhook_url="http://x", opener=fake_opener,
        )
        output = SimpleNamespace(
            title="L8 says", text="something important", level="critical",
        )
        surface.post(output)
        assert len(captured) == 1
        assert captured[0]["embeds"][0]["title"] == "[CRIT] L8 says"
        assert captured[0]["embeds"][0]["color"] == 0xE74C3C

    def test_post_falls_back_to_default_level(self):
        captured = []
        def fake_opener(req, timeout=None):
            captured.append(json.loads(req.data))
            return _OK()
        surface = discord_ping.DiscordPingSurface(
            webhook_url="http://x",
            default_level="warning",
            opener=fake_opener,
        )
        # Output without an explicit level attribute.
        surface.post(SimpleNamespace(title="t", text="b"))
        assert captured[0]["embeds"][0]["color"] == 0xF1C40F  # warning

    def test_post_with_missing_attrs_uses_defaults(self):
        captured = []
        def fake_opener(req, timeout=None):
            captured.append(json.loads(req.data))
            return _OK()
        surface = discord_ping.DiscordPingSurface(
            webhook_url="http://x", opener=fake_opener,
        )
        # Output object with literally nothing -- still doesn't crash.
        surface.post(SimpleNamespace())
        assert captured[0]["embeds"][0]["title"] == "[INFO] L8 escalation"
