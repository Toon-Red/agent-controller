"""Discord async ping for L8 escalations (AC-S16e).

The CONCRETE implementation of the ``DiscordSurface`` protocol declared
in :mod:`scripts.l8_project_manager`. L8 fires time-sensitive
escalations to Preston via a webhook -- decisions-needed when Preston
is away, blockers requiring input, material trajectory shifts.

Security: the webhook URL comes from the ``DISCORD_WEBHOOK`` env var,
NEVER hardcoded. If the env var is absent or empty, calls log a
warning and no-op so a misconfigured environment doesn't crash L8.

Idempotency: each call hashes (title|body) and skips if the same hash
was sent within ``DEDUP_WINDOW_SECONDS`` (default 3600s = 1h). The
hash cache is in-memory (per-process) -- restarts forget it, which
is the desired behaviour (fresh process == fresh user attention).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


log = logging.getLogger("agent-controller.discord_ping")

ENV_WEBHOOK = "DISCORD_WEBHOOK"
DEDUP_WINDOW_SECONDS = 3600           # default 1h
DEFAULT_TIMEOUT_SECONDS = 5

Level = Literal["info", "warning", "critical"]

# Discord embed colors (decimal ints). Visual differentiation by level.
LEVEL_COLORS: dict[str, int] = {
    "info":     0x3498DB,   # blue
    "warning":  0xF1C40F,   # yellow
    "critical": 0xE74C3C,   # red
}

# Emoji prefix per level -- ASCII-safe via plain prefix tokens (not
# emoji glyphs) so logs + tests don't choke on encoding.
LEVEL_PREFIXES: dict[str, str] = {
    "info":     "[INFO]",
    "warning":  "[WARN]",
    "critical": "[CRIT]",
}


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------

@dataclass
class _DedupCache:
    window_seconds: int = DEDUP_WINDOW_SECONDS
    _entries: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def should_skip(self, key: str, *, now: float | None = None) -> bool:
        """True if this key was seen inside the window. Side effect: prunes
        expired entries on every call."""
        now = now if now is not None else time.time()
        with self._lock:
            self._prune(now)
            if key in self._entries:
                return True
            self._entries[key] = now
            return False

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        stale = [k for k, ts in self._entries.items() if ts < cutoff]
        for k in stale:
            del self._entries[k]

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


_DEDUP = _DedupCache()


def reset_dedup_cache() -> None:
    """Test helper -- clear the module-level dedup state."""
    _DEDUP.reset()


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def _hash_key(title: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(title.encode("utf-8", errors="replace"))
    h.update(b"|")
    h.update(body.encode("utf-8", errors="replace"))
    return h.hexdigest()


def build_embed_payload(title: str, body: str, level: Level = "info",
                         *, extra_fields: list[dict[str, Any]] | None = None,
                         ) -> dict[str, Any]:
    """Build the Discord webhook payload for an escalation.

    Visual differentiation: prefix in the title + per-level embed color.
    """
    if level not in LEVEL_COLORS:
        raise ValueError(
            f"unknown level {level!r}; expected one of "
            f"{sorted(LEVEL_COLORS)}"
        )
    prefix = LEVEL_PREFIXES[level]
    color = LEVEL_COLORS[level]
    embed: dict[str, Any] = {
        "title": f"{prefix} {title}",
        "description": body,
        "color": color,
    }
    if extra_fields:
        embed["fields"] = list(extra_fields)
    return {"embeds": [embed]}


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def _post_webhook(url: str, payload: dict,
                   *, opener: Callable[..., Any] = urllib.request.urlopen,
                   timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bool:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(req, timeout=timeout) as r:
            r.read()
        return True
    except urllib.error.HTTPError as exc:
        log.warning("Discord webhook HTTP %s: %s", exc.code, exc.reason)
        return False
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
        log.warning("Discord webhook unreachable: %s", exc)
        return False


def send_escalation(title: str, body: str, level: Level = "info",
                     *, webhook_url: str | None = None,
                     extra_fields: list[dict[str, Any]] | None = None,
                     dedup: bool = True,
                     opener: Callable[..., Any] = urllib.request.urlopen,
                     ) -> bool:
    """Fire an L8 escalation ping to Discord.

    Returns True on a successful POST, False on dedup-skip, missing
    webhook, or HTTP failure (all non-crash outcomes -- the caller is
    L8 and shouldn't have its iteration broken by a ping outage).

    ``webhook_url`` overrides the ``DISCORD_WEBHOOK`` env var (used
    primarily in tests so the env var doesn't leak between tests).
    """
    url = webhook_url if webhook_url is not None else os.environ.get(ENV_WEBHOOK, "")
    if not url:
        log.warning(
            "Discord escalation skipped -- %s env var not set "
            "(title=%r, level=%r)", ENV_WEBHOOK, title, level,
        )
        return False

    if dedup:
        key = _hash_key(title, body)
        if _DEDUP.should_skip(key):
            log.info("Discord escalation skipped (dedup window): %r", title)
            return False

    try:
        payload = build_embed_payload(title, body, level,
                                        extra_fields=extra_fields)
    except ValueError as exc:
        log.warning("Discord escalation skipped -- bad payload: %s", exc)
        return False

    ok = _post_webhook(url, payload, opener=opener)
    if not ok:
        log.warning("Discord escalation FAILED to send: %r", title)
    return ok


# ---------------------------------------------------------------------------
# DiscordSurface protocol implementation (consumed by L8ProjectManager)
# ---------------------------------------------------------------------------

class DiscordPingSurface:
    """Concrete :class:`scripts.l8_project_manager.DiscordSurface` implementation.

    Constructed once at L8 startup; passed to the L8ProjectManager as
    its `discord` dependency.
    """

    def __init__(self, *, webhook_url: str | None = None,
                  default_level: Level = "info",
                  opener: Callable[..., Any] = urllib.request.urlopen):
        self.webhook_url = webhook_url
        self.default_level = default_level
        self._opener = opener

    def post(self, output) -> None:
        """L8Output -> Discord. Maps output.tag to level when present;
        falls back to ``default_level``."""
        title = getattr(output, "title", None) or "L8 escalation"
        body = getattr(output, "text", None) or ""
        # L8Output may carry a `level` attribute (per AC-S16e contract);
        # accept "info" | "warning" | "critical" or default.
        level = getattr(output, "level", None) or self.default_level
        if level not in LEVEL_COLORS:
            level = self.default_level
        send_escalation(
            title, body, level,
            webhook_url=self.webhook_url, opener=self._opener,
        )
