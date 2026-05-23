"""HumanEngine: Preston-on-the-keyboard validation harness (09ca6f69).

NOT a production engine. Selectable via the same engines.yaml
mechanism from ``4ae126d2`` for one specific purpose: proving that
an L assignment is REASONABLE by proving a human could execute it.
If Preston can sit down, read the prompt, and produce the expected
output -- the prompt structure is good enough for an LLM.

Flow:
  1. ``run(prompt)`` writes the prompt + a response template to
     ``data/human-engine/<ts>/<level>-<role>.md``.
  2. Discord ping (best-effort) tells Preston where the file is.
  3. Polls every ``poll_interval_sec`` for
     ``...<level>-<role>.response.md`` to appear.
  4. Parses the response file's YAML frontmatter ``outputs:`` block
     against the prompt's ``task_block.outputs``.
  5. Returns the parsed response dict OR raises
     ``HumanEngineTimeout`` after ``timeout_sec``.

Sibling registry: per the dispatch guardrail, this does NOT retrofit
the existing planning/scheduling/learning engines from ``4ae126d2``
(those are EOD-step engines, different concept). HumanEngine is the
first entry in a new ``AGENT_ENGINE_REGISTRY`` -- agent engines
implement the same ``run(prompt) -> dict`` shape but operate on
LPrompts rather than EOD findings.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from scripts.l_prompt import LPrompt, validate_or_raise


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESPONSE_DIR = REPO_ROOT / "data" / "human-engine"


class HumanEngineTimeout(BaseException):
    """Raised when the response file doesn't appear within
    ``timeout_sec``. ``BaseException`` so a stray ``except Exception``
    in the orchestrator runtime can't swallow the deadline."""


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _prompt_filename(prompt: LPrompt) -> str:
    return f"{prompt.get('level', '?')}-{prompt.get('agent_role', '?')}.md"


def _render_prompt_md(prompt: LPrompt) -> str:
    """Render the LPrompt as a markdown file Preston can read."""
    lines = [
        "---",
        f"level: {prompt.get('level')}",
        f"agent_role: {prompt.get('agent_role')}",
        f"generated_at: {_ts()}",
        "---",
        "",
        f"# {prompt.get('level')} {prompt.get('agent_role')} prompt",
        "",
        "## context_block",
        "```json",
        json.dumps(prompt.get("context_block", {}), indent=2, ensure_ascii=False,
                   default=str),
        "```",
        "",
        "## task_block",
        "```json",
        json.dumps(prompt.get("task_block", {}), indent=2, ensure_ascii=False,
                   default=str),
        "```",
        "",
        "## tools_block",
        "```",
        "\n".join(prompt.get("tools_block", [])),
        "```",
        "",
        "## examples_block",
        "```json",
        json.dumps(prompt.get("examples_block", []), indent=2,
                   ensure_ascii=False, default=str),
        "```",
        "",
        "---",
        "",
        "## To respond",
        "",
        "Write the response to the sibling file with `.response.md` "
        "instead of `.md`. The body must include a frontmatter "
        "`outputs:` block matching the prompt's `task_block.outputs` keys.",
    ]
    return "\n".join(lines) + "\n"


def _parse_response(path: Path) -> dict:
    """Parse the response file's YAML frontmatter ``outputs:`` block."""
    try:
        import yaml  # type: ignore
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                front = text[3:end].strip()
                parsed = yaml.safe_load(front) or {}
                if isinstance(parsed, Mapping):
                    out = parsed.get("outputs")
                    if isinstance(out, Mapping):
                        return dict(out)
        return {"raw_body": text}
    except Exception as exc:
        return {"_parse_error": f"{type(exc).__name__}: {exc}",
                "raw_body": path.read_text(encoding="utf-8", errors="replace")}


@dataclass
class HumanEngine:
    """Selectable agent engine that delegates to Preston-at-keyboard.

    ``base_dir`` injectable for tests; defaults to
    ``data/human-engine/``. ``ping`` callable defaults to a no-op so
    tests don't fire Discord.
    """
    base_dir: Optional[Path] = None
    poll_interval_sec: float = 1.0
    ping: Optional[Callable[[str, str], None]] = None

    def _root(self) -> Path:
        return self.base_dir or DEFAULT_RESPONSE_DIR

    def run(self, prompt: LPrompt, *, timeout_sec: int = 600) -> dict:
        """Write prompt; wait for response; return parsed outputs.

        Validates the prompt first via ``validate_or_raise`` so a
        broken prompt never leaves the building. Times out via
        ``HumanEngineTimeout`` so the calling orchestrator doesn't
        block forever on a missing Preston.
        """
        validate_or_raise(prompt, level=prompt.get("level", "?"),
                          role=prompt.get("agent_role", "?"))
        ts = _ts()
        run_dir = self._root() / ts
        run_dir.mkdir(parents=True, exist_ok=True)
        fname = _prompt_filename(prompt)
        prompt_path = run_dir / fname
        response_path = run_dir / fname.replace(".md", ".response.md")
        prompt_path.write_text(_render_prompt_md(prompt), encoding="utf-8")
        # Best-effort Discord ping.
        if self.ping is not None:
            try:
                self.ping(
                    f"HumanEngine waiting on {prompt.get('level')}-"
                    f"{prompt.get('agent_role')}",
                    str(prompt_path),
                )
            except Exception:
                pass
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if response_path.is_file():
                return _parse_response(response_path)
            time.sleep(self.poll_interval_sec)
        raise HumanEngineTimeout(
            f"no response at {response_path} after {timeout_sec}s "
            f"(level={prompt.get('level')}, role={prompt.get('agent_role')})"
        )


# ---------------------------------------------------------------------------
# Agent-engine registry (sibling to 4ae126d2's planning/scheduling/learning)
# ---------------------------------------------------------------------------

AGENT_ENGINE_REGISTRY: dict[str, Callable[..., Any]] = {
    "HumanEngine": HumanEngine,
}


def get_agent_engine(name: str, **kwargs) -> Any:
    """Resolve an agent engine class by name + instantiate. Mirrors
    the ``engines.registry`` pattern from 4ae126d2."""
    cls = AGENT_ENGINE_REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"unknown agent engine {name!r}; known: "
            f"{sorted(AGENT_ENGINE_REGISTRY)}"
        )
    return cls(**kwargs)
