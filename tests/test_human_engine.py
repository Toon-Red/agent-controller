"""Unit tests for AC-S14 -- human engine option + mid-flight AI handoff.

Coverage:
- Loader accepts `engine: human` + handoff_to + handoff_trigger.
- Loader rejects handoff fields on non-human engines (foot-gun guard).
- HumanDriver pause/resume cycle: prompt+context surfaced byte-for-byte,
  user response returned, telemetry source="human", attribution counts
  human bytes.
- Handoff path (on_keyword): partial passed as additional context to
  the configured AI driver, merged response returned with
  source="mixed" and attribution split between human and ai.
- Handoff path (on_timer): same shape, trigger="timer".
- Unknown handoff target surfaces a clear error.
- Settings.resolve_engine returns 'human' for a human-configured role.

Tests deliberately avoid stdin/stdout I/O so they're hermetic; the
real stdin/stdout glue ships in scripts.engine_driver._stdin_input_provider
and is exercised manually + by the Dream tab in a follow-up.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.engine_driver import (
    BaseDriver,
    Dispatcher,
    EngineRequest,
    EngineResponse,
    HumanDriver,
    UnknownEngineError,
    keyword_aware_input_provider,
    scripted_input_provider,
    timed_input_provider,
)
from scripts.role_config import (
    HUMAN_ENGINE,
    RoleConfigError,
    load_settings,
    parse_settings,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Loader: schema acceptance
# ---------------------------------------------------------------------------


def test_loader_accepts_human_engine_with_keyword_handoff():
    settings = parse_settings(
        {
            "engines": {"L5": "claude-haiku"},
            "roles": {
                "inspector": {
                    "level": "L5",
                    "engine": "human",
                    "handoff_to": "claude-sonnet",
                    "handoff_trigger": {"on_keyword": "/continue"},
                }
            },
        }
    )
    cfg = settings.roles["inspector"]
    assert cfg.is_human
    assert cfg.engine == HUMAN_ENGINE
    assert cfg.handoff_to == "claude-sonnet"
    assert cfg.handoff_trigger == {"on_keyword": "/continue"}


def test_loader_accepts_human_engine_with_timer_handoff():
    settings = parse_settings(
        {
            "engines": {"L7": "claude-opus"},
            "roles": {
                "dispatch": {
                    "level": "L7",
                    "engine": "human",
                    "handoff_to": "claude-opus",
                    "handoff_trigger": {"on_timer": 600},
                }
            },
        }
    )
    cfg = settings.roles["dispatch"]
    assert cfg.is_human
    assert cfg.handoff_trigger == {"on_timer": 600}


def test_loader_accepts_human_engine_without_handoff():
    settings = parse_settings(
        {
            "engines": {"L4": "ollama-local"},
            "roles": {
                "coder": {"level": "L4", "engine": "human"},
            },
        }
    )
    cfg = settings.roles["coder"]
    assert cfg.is_human
    assert cfg.handoff_to is None
    assert cfg.handoff_trigger is None


def test_resolve_engine_returns_human_for_human_role():
    settings = parse_settings(
        {
            "engines": {"L5": "claude-haiku"},
            "roles": {"inspector": {"level": "L5", "engine": "human"}},
        }
    )
    assert settings.resolve_engine("inspector") == "human"


def test_loader_rejects_handoff_fields_on_non_human_engine():
    with pytest.raises(RoleConfigError, match="only valid when engine='human'"):
        parse_settings(
            {
                "engines": {"L4": "ollama-local"},
                "roles": {
                    "coder": {
                        "level": "L4",
                        "engine": "ollama-local",
                        "handoff_to": "claude-sonnet",
                    }
                },
            }
        )


def test_loader_rejects_handoff_trigger_with_both_keyword_and_timer():
    with pytest.raises(RoleConfigError, match="exactly one of"):
        parse_settings(
            {
                "roles": {
                    "x": {
                        "level": "L5",
                        "engine": "human",
                        "handoff_trigger": {"on_keyword": "/c", "on_timer": 600},
                    }
                }
            }
        )


def test_loader_rejects_handoff_trigger_with_empty_keyword():
    with pytest.raises(RoleConfigError, match="non-empty string"):
        parse_settings(
            {
                "roles": {
                    "x": {
                        "level": "L5",
                        "engine": "human",
                        "handoff_trigger": {"on_keyword": ""},
                    }
                }
            }
        )


def test_loader_rejects_handoff_trigger_with_non_positive_timer():
    with pytest.raises(RoleConfigError, match="positive number"):
        parse_settings(
            {
                "roles": {
                    "x": {
                        "level": "L5",
                        "engine": "human",
                        "handoff_trigger": {"on_timer": 0},
                    }
                }
            }
        )


def test_loader_rejects_missing_level():
    with pytest.raises(RoleConfigError, match="level"):
        parse_settings({"roles": {"x": {"engine": "human"}}})


def test_repo_template_settings_loads_with_human_role():
    """The shipped templates/settings.json must validate AND include
    the AC-S14 human-engine example so future contributors have a
    working reference."""
    settings = load_settings(ROOT / "templates" / "settings.json")
    human_roles = [r for r in settings.roles.values() if r.is_human]
    assert human_roles, "templates/settings.json must ship an engine=human example"
    sample = human_roles[0]
    assert sample.handoff_to, "the human-engine sample must show a handoff_to wiring"
    assert sample.handoff_trigger, "the human-engine sample must show a handoff_trigger"

    # Sanity: the JSON is valid and machine-readable end-to-end.
    raw = json.loads((ROOT / "templates" / "settings.json").read_text(encoding="utf-8"))
    assert "engines" in raw and "roles" in raw


# ---------------------------------------------------------------------------
# HumanDriver: pause/resume (no handoff)
# ---------------------------------------------------------------------------


def test_human_driver_pause_resume_cycle_surfaces_full_payload():
    """Pause/resume: the user sees the EXACT prompt+context (no
    summarisation) and their typed response becomes the role output."""
    surfaced: list[str] = []
    driver = HumanDriver(
        surface=surfaced.append,
        input_provider=scripted_input_provider("the human's full answer"),
    )

    request = EngineRequest(
        prompt="You are the L5 grader. Score this PR.",
        context="diff: +foo\n-bar\nfile.py changed",
        role="grader",
        level="L5",
    )
    response = driver.run(request)

    # Byte-for-byte: the surfaced payload contains BOTH prompt and
    # context unmodified.
    assert len(surfaced) == 1
    payload = surfaced[0]
    assert request.prompt in payload
    assert request.context in payload
    assert "[role=grader level=L5]" in payload
    assert "=== PROMPT ===" in payload
    assert "=== CONTEXT ===" in payload

    # Resume: response carries the human text + correct telemetry.
    assert response.text == "the human's full answer"
    assert response.source == "human"
    assert response.attribution == {
        "human": len("the human's full answer".encode("utf-8"))
    }
    assert response.handoff_to is None
    assert response.handoff_trigger is None


def test_human_driver_without_context_omits_context_section():
    surfaced: list[str] = []
    HumanDriver(
        surface=surfaced.append,
        input_provider=scripted_input_provider("answer"),
    ).run(EngineRequest(prompt="P"))
    assert "=== PROMPT ===" in surfaced[0]
    assert "=== CONTEXT ===" not in surfaced[0]


# ---------------------------------------------------------------------------
# HumanDriver: handoff path
# ---------------------------------------------------------------------------


class _StubAIDriver(BaseDriver):
    """A scriptable AI driver used to verify handoff plumbing.

    Captures the request it was handed so the test can assert that
    the human's partial + upstream context propagated correctly.
    """

    engine_id = "stub-ai"

    def __init__(self, reply: str = "...AI continuation.") -> None:
        self.reply = reply
        self.last_request: EngineRequest | None = None
        self.call_count = 0

    def run(self, request: EngineRequest) -> EngineResponse:
        self.last_request = request
        self.call_count += 1
        return EngineResponse(
            text=self.reply,
            source="ai",
            attribution={"ai": len(self.reply.encode("utf-8"))},
        )


def test_human_driver_handoff_on_keyword_routes_partial_to_ai():
    """The handoff path: keyword fires, partial is forwarded as
    additional context to the AI driver, merged output is returned
    with source='mixed' and split attribution."""
    ai = _StubAIDriver(reply="AI: here's the rest.")
    dispatcher = Dispatcher()
    dispatcher.register("stub-ai", ai)

    # The human types two lines, then `/continue` to hand off.
    lines = ["I'll start the answer.\n", "Here's my partial thought:\n", "/continue\n"]
    driver = HumanDriver(
        handoff_to="stub-ai",
        handoff_trigger={"on_keyword": "/continue"},
        surface=lambda _: None,
        input_provider=keyword_aware_input_provider(lines),
        dispatcher=dispatcher,
    )

    request = EngineRequest(
        prompt="Grade this PR.",
        context="upstream diff bytes here",
        role="grader",
        level="L5",
    )
    response = driver.run(request)

    # The AI was actually called.
    assert ai.call_count == 1

    # The AI saw the upstream context AND the human's partial framed
    # as a handoff -- not a fresh start.
    forwarded = ai.last_request
    assert forwarded is not None
    assert "upstream diff bytes here" in forwarded.context
    assert "I'll start the answer." in forwarded.context
    assert "Here's my partial thought:" in forwarded.context
    assert "/continue" not in forwarded.context  # keyword is consumed, not forwarded
    assert "=== HUMAN HANDOFF ===" in forwarded.context
    assert forwarded.extras.get("handoff_from") == "human"

    # Merged response: partial + AI continuation.
    assert response.text.startswith("I'll start the answer.")
    assert response.text.endswith("AI: here's the rest.")
    assert response.source == "mixed"
    assert response.handoff_to == "stub-ai"
    assert response.handoff_trigger == "keyword"

    # Telemetry split: human bytes + ai bytes accounted for.
    partial = "I'll start the answer.\nHere's my partial thought:\n"
    assert response.attribution["human"] == len(partial.encode("utf-8"))
    assert response.attribution["ai"] == len("AI: here's the rest.".encode("utf-8"))


def test_human_driver_handoff_on_timer_routes_partial_to_ai():
    """on_timer trigger fires when the simulated work time exceeds
    the configured timeout. Same merge semantics as on_keyword."""
    ai = _StubAIDriver(reply="AI continues after timeout.")
    dispatcher = Dispatcher()
    dispatcher.register("stub-ai", ai)

    driver = HumanDriver(
        handoff_to="stub-ai",
        handoff_trigger={"on_timer": 5},
        surface=lambda _: None,
        input_provider=timed_input_provider(
            text_before_timeout="partial text", work_time=10.0
        ),
        dispatcher=dispatcher,
    )

    response = driver.run(
        EngineRequest(prompt="prompt", context="ctx", role="dispatch", level="L7")
    )

    assert ai.call_count == 1
    assert response.source == "mixed"
    assert response.handoff_trigger == "timer"
    assert response.text == "partial text\nAI continues after timeout."
    assert response.attribution["human"] == len("partial text".encode("utf-8"))
    assert response.attribution["ai"] == len(
        "AI continues after timeout.".encode("utf-8")
    )


def test_human_driver_timer_not_tripped_returns_pure_human():
    """Symmetric path: same provider, but work_time < timeout, so
    the AI is NEVER consulted and telemetry stays human-only."""
    ai = _StubAIDriver()
    dispatcher = Dispatcher()
    dispatcher.register("stub-ai", ai)

    driver = HumanDriver(
        handoff_to="stub-ai",
        handoff_trigger={"on_timer": 60},
        surface=lambda _: None,
        input_provider=timed_input_provider(
            text_before_timeout="finished in time", work_time=10.0
        ),
        dispatcher=dispatcher,
    )
    response = driver.run(EngineRequest(prompt="p"))
    assert ai.call_count == 0
    assert response.source == "human"
    assert response.text == "finished in time"


def test_human_driver_keyword_not_tripped_returns_pure_human():
    """User finished without typing the keyword -> pure human output."""
    ai = _StubAIDriver()
    dispatcher = Dispatcher()
    dispatcher.register("stub-ai", ai)
    driver = HumanDriver(
        handoff_to="stub-ai",
        handoff_trigger={"on_keyword": "/continue"},
        surface=lambda _: None,
        input_provider=keyword_aware_input_provider(["all done\n"]),
        dispatcher=dispatcher,
    )
    response = driver.run(EngineRequest(prompt="p"))
    assert ai.call_count == 0
    assert response.source == "human"
    assert response.text == "all done\n"


# ---------------------------------------------------------------------------
# Driver wiring / error paths
# ---------------------------------------------------------------------------


def test_handoff_to_without_dispatcher_is_rejected_eagerly():
    with pytest.raises(ValueError, match="dispatcher"):
        HumanDriver(handoff_to="stub-ai")


def test_handoff_to_unknown_engine_surfaces_clear_error():
    dispatcher = Dispatcher()  # nothing registered
    driver = HumanDriver(
        handoff_to="not-registered",
        handoff_trigger={"on_keyword": "/continue"},
        surface=lambda _: None,
        input_provider=keyword_aware_input_provider(["partial\n", "/continue\n"]),
        dispatcher=dispatcher,
    )
    with pytest.raises(UnknownEngineError, match="not-registered"):
        driver.run(EngineRequest(prompt="p"))


def test_dispatcher_resolves_registered_human_driver():
    dispatcher = Dispatcher()
    human = HumanDriver(
        surface=lambda _: None,
        input_provider=scripted_input_provider("ok"),
    )
    dispatcher.register("human", human)
    assert dispatcher.resolve("human") is human
    assert "human" in dispatcher.known()
