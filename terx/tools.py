"""Persisted, policy-bound tool manifests and safe live result extraction.

The manifest is deliberately data-only: it contains no browser script, raw
input value, or provider credential.  A saved tool can therefore be discovered
through MCP after a browser restart, while each invocation still supplies the
caller scope and current input values.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from terx.cdp.bridge import CDPBridge

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_SENSITIVE_INPUT = re.compile(r"(?:pass(?:word)?|secret|token|api[_-]?key|credential)", re.I)
_SOURCES = {"input", "text", "title", "url"}


class ToolManifestError(ValueError):
    """Raised when a saved tool's public contract is malformed."""


class ToolResultError(RuntimeError):
    """Raised when a saved tool cannot read its declared live result."""


def _require_field_name(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _FIELD_NAME.fullmatch(value) is None:
        raise ToolManifestError(f"{label} must be a lower-snake-case identifier")


def validate_tool_inputs(input_names: tuple[str, ...], inputs: Mapping[str, Any]) -> dict[str, str]:
    """Require every declared text input and return an isolated string mapping."""
    if not isinstance(inputs, Mapping):
        raise ToolManifestError("tool inputs must be an object")
    unexpected = sorted(set(inputs) - set(input_names))
    missing = sorted(set(input_names) - set(inputs))
    if missing or unexpected:
        parts: list[str] = []
        if missing:
            parts.append(f"missing inputs: {', '.join(missing)}")
        if unexpected:
            parts.append(f"unexpected inputs: {', '.join(unexpected)}")
        raise ToolManifestError("; ".join(parts))
    if not all(isinstance(value, (str, int, float, bool)) for value in inputs.values()):
        raise ToolManifestError("tool input values must be scalar values")
    return {name: str(inputs[name]) for name in input_names}


def validate_result_spec(
    result_spec: Mapping[str, Mapping[str, str]], input_names: tuple[str, ...]
) -> dict[str, dict[str, str]]:
    """Validate TERX's small, data-only fresh-result schema.

    ``text`` reads visible text from one CSS selector. ``title`` and ``url``
    are page metadata. ``input`` can echo a non-sensitive supplied input, which
    is useful for correlating a result with an order identifier.  Arbitrary
    JavaScript and form values are intentionally not part of the schema.
    """
    if not isinstance(result_spec, Mapping) or not result_spec:
        raise ToolManifestError("result_spec must be a non-empty object")

    normalized: dict[str, dict[str, str]] = {}
    for field, raw_rule in result_spec.items():
        _require_field_name(field, label="result field")
        if not isinstance(raw_rule, Mapping):
            raise ToolManifestError(f"result field {field!r} must be an object")
        source = raw_rule.get("source")
        if source not in _SOURCES:
            raise ToolManifestError(
                f"result field {field!r} source must be one of {', '.join(sorted(_SOURCES))}"
            )
        rule = {str(key): str(value) for key, value in raw_rule.items()}
        allowed = {"source"}
        if source == "text":
            allowed.add("selector")
            selector = raw_rule.get("selector")
            if not isinstance(selector, str) or not selector or len(selector) > 512:
                raise ToolManifestError(
                    f"result field {field!r} needs a CSS selector up to 512 chars"
                )
        elif source == "input":
            allowed.add("name")
            name = raw_rule.get("name")
            if not isinstance(name, str) or name not in input_names:
                raise ToolManifestError(
                    f"result field {field!r} must reference one declared input name"
                )
            if _SENSITIVE_INPUT.search(name):
                raise ToolManifestError("sensitive inputs cannot be returned in a tool result")
        if set(raw_rule) != allowed:
            raise ToolManifestError(
                f"result field {field!r} accepts only {', '.join(sorted(allowed))}"
            )
        normalized[field] = rule
    return normalized


@dataclass(frozen=True)
class SavedTool:
    """A compact, serializable contract for a reusable TERX browser tool."""

    name: str
    description: str
    task: str
    input_names: tuple[str, ...]
    scope_hash: str
    precondition: dict[str, Any]
    postcondition: dict[str, Any]
    route_pattern: str | None
    workflow_version: int
    side_effect: Literal["read_only", "mutating", "destructive"]
    ttl_seconds: int | None
    mutation_guard: bool
    mutation_threshold: int
    result_spec: dict[str, dict[str, str]]

    def __post_init__(self) -> None:
        _require_field_name(self.name, label="tool name")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ToolManifestError("tool description must be non-empty")
        if not isinstance(self.task, str) or not self.task.strip():
            raise ToolManifestError("tool task must be non-empty")
        if len(set(self.input_names)) != len(self.input_names):
            raise ToolManifestError("tool input names must be unique")
        for input_name in self.input_names:
            _require_field_name(input_name, label="tool input name")
        if not isinstance(self.scope_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.scope_hash
        ):
            raise ToolManifestError("tool scope_hash must be a SHA-256 digest")
        if not isinstance(self.precondition, dict) or not self.precondition:
            raise ToolManifestError("tool precondition must be a non-empty object")
        if not isinstance(self.postcondition, dict) or not self.postcondition:
            raise ToolManifestError("tool postcondition must be a non-empty object")
        if self.side_effect not in {"read_only", "mutating", "destructive"}:
            raise ToolManifestError("tool side_effect is invalid")
        if not isinstance(self.workflow_version, int) or self.workflow_version < 1:
            raise ToolManifestError("tool workflow_version must be a positive integer")
        if self.ttl_seconds is not None and (
            not isinstance(self.ttl_seconds, int) or self.ttl_seconds <= 0
        ):
            raise ToolManifestError("tool ttl_seconds must be positive or null")
        if not isinstance(self.mutation_threshold, int) or self.mutation_threshold < 0:
            raise ToolManifestError("tool mutation_threshold must be a non-negative integer")
        normalized = validate_result_spec(self.result_spec, self.input_names)
        object.__setattr__(self, "result_spec", normalized)

    def as_dict(self) -> dict[str, Any]:
        """Return a safe discovery payload; raw scope and values are absent."""
        return {
            "name": self.name,
            "description": self.description,
            "task": self.task,
            "input_schema": {
                "type": "object",
                "properties": {name: {"type": "string"} for name in self.input_names},
                "required": list(self.input_names),
                "additionalProperties": False,
            },
            "result_schema": {
                "type": "object",
                "properties": {name: {"type": "string"} for name in self.result_spec},
                "required": list(self.result_spec),
                "additionalProperties": False,
            },
            "scope_digest": self.scope_hash,
            "route_pattern": self.route_pattern,
            "workflow_version": self.workflow_version,
            "side_effect": self.side_effect,
            "ttl_seconds": self.ttl_seconds,
        }


async def extract_tool_result(
    bridge: CDPBridge, result_spec: Mapping[str, Mapping[str, str]], inputs: Mapping[str, Any]
) -> dict[str, str]:
    """Read a validated live result without recording a replay action.

    All CDP calls use ``send_internal``.  The only generated JavaScript is a
    fixed read-only text lookup; the CSS selector is JSON encoded rather than
    interpolated as code.
    """
    normalized_inputs = {name: str(value) for name, value in inputs.items()}
    normalized_spec = validate_result_spec(result_spec, tuple(normalized_inputs))
    result: dict[str, str] = {}
    for field, rule in normalized_spec.items():
        source = rule["source"]
        if source == "input":
            result[field] = normalized_inputs[rule["name"]]
            continue
        if source == "url":
            expression = "window.location.href"
        elif source == "title":
            expression = "document.title"
        else:
            selector = json.dumps(rule["selector"])
            expression = (
                "(() => { const element = document.querySelector(" + selector + "); "
                "return element ? String(element.innerText ?? element.textContent ?? '') : null; })()"
            )
        raw = await bridge.send_internal(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}
        )
        value = raw.get("result", {}).get("value")
        if not isinstance(value, str):
            raise ToolResultError(
                f"tool result field {field!r} could not be read from the current page"
            )
        result[field] = value
    return result


async def call_result_reader(reader: Any, bridge: CDPBridge) -> Any:
    """Run a host-owned reader once, accepting sync or async callables."""
    value = reader(bridge)
    if inspect.isawaitable(value):
        value = await value
    return value
