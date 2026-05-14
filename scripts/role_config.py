"""Role-config loader for templates/settings.json.

AC-S2 lands the full schema; AC-S14 only needs the loader to accept
``engine: human`` (plus the optional ``handoff_to`` and
``handoff_trigger`` companion fields) without error. This module
delivers exactly that subset and is designed to be extended by AC-S2
without breaking the human-engine contract.

Schema accepted (informally; the full v2 schema lands in AC-S2):

    {
      "engines": {                   # layer defaults
        "L4": "ollama-local",
        "L5": "claude-haiku",
        "L6": "claude-sonnet",
        "L7": "claude-opus",
        "L8": "claude-opus"
      },
      "roles": {                     # per-role overrides
        "<role-id>": {
          "level": "L4" | ... | "L8",
          "engine": "<engine-id>" | "human",
          "handoff_to": "<engine-id>",          # human-engine only
          "handoff_trigger": {                  # human-engine only
            "on_keyword": "/continue"           #   exactly one of
                                                #   on_keyword or on_timer
            "on_timer": 600
          }
        }
      }
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

VALID_LEVELS = {"L4", "L5", "L6", "L7", "L8"}
HUMAN_ENGINE = "human"


class RoleConfigError(ValueError):
    """Raised when templates/settings.json fails validation."""


@dataclass(frozen=True)
class RoleConfig:
    role: str
    level: str
    engine: str
    handoff_to: Optional[str] = None
    handoff_trigger: Optional[Mapping[str, Any]] = None

    @property
    def is_human(self) -> bool:
        return self.engine == HUMAN_ENGINE


@dataclass
class Settings:
    layer_defaults: dict[str, str] = field(default_factory=dict)
    roles: dict[str, RoleConfig] = field(default_factory=dict)

    def resolve_engine(self, role_id: str) -> str:
        """Return the effective engine for ``role_id``.

        Per-role override > layer default. AC-S9 owns the full
        resolver semantics; this minimal version is sufficient for
        the human-engine path.
        """
        if role_id not in self.roles:
            raise RoleConfigError(f"unknown role {role_id!r}")
        cfg = self.roles[role_id]
        if cfg.engine:
            return cfg.engine
        try:
            return self.layer_defaults[cfg.level]
        except KeyError as exc:
            raise RoleConfigError(
                f"no engine override for role {role_id!r} and no layer "
                f"default for {cfg.level}"
            ) from exc


def load_settings(path: str | Path) -> Settings:
    """Load and validate ``templates/settings.json``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_settings(data)


def parse_settings(data: Mapping[str, Any]) -> Settings:
    if not isinstance(data, Mapping):
        raise RoleConfigError("settings root must be an object")

    raw_engines = data.get("engines") or {}
    if not isinstance(raw_engines, Mapping):
        raise RoleConfigError("'engines' must be an object")
    layer_defaults: dict[str, str] = {}
    for layer, engine_id in raw_engines.items():
        if layer not in VALID_LEVELS:
            raise RoleConfigError(
                f"engines.{layer}: unknown level (expected one of "
                f"{sorted(VALID_LEVELS)})"
            )
        if not isinstance(engine_id, str) or not engine_id:
            raise RoleConfigError(f"engines.{layer}: engine must be a non-empty string")
        layer_defaults[layer] = engine_id

    raw_roles = data.get("roles") or {}
    if not isinstance(raw_roles, Mapping):
        raise RoleConfigError("'roles' must be an object")

    roles: dict[str, RoleConfig] = {}
    for role_id, role_data in raw_roles.items():
        roles[role_id] = _parse_role(role_id, role_data)

    return Settings(layer_defaults=layer_defaults, roles=roles)


def _parse_role(role_id: str, role_data: Any) -> RoleConfig:
    if not isinstance(role_data, Mapping):
        raise RoleConfigError(f"roles.{role_id}: must be an object")

    level = role_data.get("level")
    if level not in VALID_LEVELS:
        raise RoleConfigError(
            f"roles.{role_id}.level: required, must be one of {sorted(VALID_LEVELS)}"
        )

    engine = role_data.get("engine")
    if engine is not None and (not isinstance(engine, str) or not engine):
        raise RoleConfigError(
            f"roles.{role_id}.engine: must be a non-empty string when present"
        )

    handoff_to = role_data.get("handoff_to")
    handoff_trigger = role_data.get("handoff_trigger")

    # Handoff fields are only meaningful for the human engine. Reject
    # them explicitly when the engine is something else to keep the
    # config honest -- a silently ignored field is a foot-gun.
    if engine != HUMAN_ENGINE:
        if handoff_to is not None or handoff_trigger is not None:
            raise RoleConfigError(
                f"roles.{role_id}: handoff_to / handoff_trigger are only "
                f"valid when engine='human' (got engine={engine!r})"
            )
    else:
        if handoff_to is not None and (not isinstance(handoff_to, str) or not handoff_to):
            raise RoleConfigError(
                f"roles.{role_id}.handoff_to: must be a non-empty engine id"
            )
        if handoff_trigger is not None:
            _validate_handoff_trigger(role_id, handoff_trigger)

    return RoleConfig(
        role=role_id,
        level=level,
        engine=engine or "",
        handoff_to=handoff_to,
        handoff_trigger=dict(handoff_trigger) if handoff_trigger else None,
    )


def _validate_handoff_trigger(role_id: str, trigger: Any) -> None:
    if not isinstance(trigger, Mapping):
        raise RoleConfigError(
            f"roles.{role_id}.handoff_trigger: must be an object"
        )
    if "on_keyword" in trigger and "on_timer" in trigger:
        raise RoleConfigError(
            f"roles.{role_id}.handoff_trigger: pick exactly one of "
            "on_keyword or on_timer, not both"
        )
    if not ({"on_keyword", "on_timer"} & set(trigger.keys())):
        raise RoleConfigError(
            f"roles.{role_id}.handoff_trigger: must declare on_keyword or on_timer"
        )
    keyword = trigger.get("on_keyword")
    if keyword is not None and (not isinstance(keyword, str) or not keyword):
        raise RoleConfigError(
            f"roles.{role_id}.handoff_trigger.on_keyword: must be a non-empty string"
        )
    timer = trigger.get("on_timer")
    if timer is not None:
        if isinstance(timer, bool) or not isinstance(timer, (int, float)) or timer <= 0:
            raise RoleConfigError(
                f"roles.{role_id}.handoff_trigger.on_timer: must be a positive number "
                "of seconds"
            )
