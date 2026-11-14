"""Placeholders, variable handling, and secret redaction at every persistence boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

from terx.cache.errors import MissingReplayVariable
from terx.cache.policy import PLACEHOLDER_RE as PLACEHOLDER_RE
from terx.cache.policy import REPLAY_ACTIONS, validate_action
from terx.cache.models import CDPCommand


logger = logging.getLogger(__name__)


SENSITIVE_LABEL_PARTS = (
    "password",
    "passcode",
    "secret",
    "token",
    "api key",
    "apikey",
    "access key",
    "private key",
    "credit card",
    "card number",
    "cvv",
    "cvc",
    "ssn",
    "social security",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("_", " ").replace("-", " ")
    return any(part in normalized for part in _sensitive_label_parts())


def _sanitize_value(value: Any, key_hint: str = "") -> Any:
    """Drop secrets from persistence and audit output at every boundary."""
    if _is_sensitive_key(key_hint):
        return "{{redacted}}"
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, key_hint) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, key_hint) for item in value]
    return value


def _sanitize_command_for_storage(command: CDPCommand) -> dict[str, Any]:
    """Serialize only replayable semantic actions with no response payloads."""
    if command.method not in REPLAY_ACTIONS:
        raise ValueError(f"TERX only persists semantic replay actions, got {command.method}")
    validate_action(command.method, command.params)
    metadata = {
        key: value
        for key, value in _sanitize_value(command.metadata).items()
        if key in {"redacted", "placeholder", "placeholder_source"}
    }
    return {
        "method": command.method,
        "params": _sanitize_value(command.params),
        "result": {},
        "latency_ms": command.latency_ms,
        "metadata": metadata,
    }


def _replay_command_digest(commands: list[CDPCommand]) -> str:
    """Return a stable, secret-safe identity for selected replay actions.

    The cache has already validated these commands as TERX's semantic IR. The
    digest intentionally excludes transient result payloads, latency, and
    recording metadata: only executable method/parameter pairs identify the
    destructive sequence a host is being asked to approve.
    """
    executable_actions = [
        {"method": command.method, "params": _sanitize_value(command.params)}
        for command in commands
    ]
    canonical = json.dumps(
        executable_actions,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _prepare_recorded_params(
    method: str,
    params: dict[str, Any],
    active_node_info: dict[str, str] | None,
    variables: dict[str, Any],
    redact_secrets: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace variable values and sensitive text with placeholders before caching."""
    metadata: dict[str, Any] = {}
    if method != "Input.insertText" or "text" not in params:
        return params, metadata

    text = params.get("text")
    if not isinstance(text, str):
        return params, metadata

    placeholder = _placeholder_for_value(text, variables)
    source = "variable"

    if placeholder is None and redact_secrets:
        if _redact_all_text_enabled():
            placeholder = _placeholder_for_node(active_node_info)
            source = "redact-all"
        elif _is_sensitive_input(active_node_info):
            placeholder = _placeholder_for_node(active_node_info)
            source = "sensitive-field"

    if placeholder is None:
        return params, metadata

    redacted = dict(params)
    redacted["text"] = placeholder
    metadata["redacted"] = True
    metadata["placeholder"] = placeholder
    metadata["placeholder_source"] = source
    if active_node_info:
        metadata["input_info"] = active_node_info
    return redacted, metadata


def _placeholder_for_value(value: str, variables: dict[str, Any]) -> str | None:
    for name, var_value in variables.items():
        if value == str(var_value):
            return f"{{{{{name}}}}}"
    return None


def _placeholder_for_node(node_info: dict[str, str] | None) -> str:
    label = (node_info or {}).get("label", "")
    return f"{{{{{_normalize_variable_name(label) or 'secret'}}}}}"


def _normalize_variable_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    if not normalized:
        return ""
    if normalized[0].isdigit():
        normalized = f"v_{normalized}"
    return normalized


def _normalize_variables(variables: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for key, value in variables.items():
        safe_key = _normalize_variable_name(str(key))
        if safe_key:
            normalized[safe_key] = value
        elif str(key):
            normalized[str(key)] = value
    return normalized


def _is_sensitive_input(node_info: dict[str, str] | None) -> bool:
    if not node_info:
        return False
    haystack = f"{node_info.get('role', '')} {node_info.get('label', '')}".lower()
    return any(part in haystack for part in _sensitive_label_parts())


def _redact_all_text_enabled() -> bool:
    return os.environ.get("TERX_REDACT_ALL_TEXT", "").lower() in {"1", "true", "yes", "on"}


def _sensitive_label_parts() -> tuple[str, ...]:
    extra = tuple(
        part.strip().lower()
        for part in os.environ.get("TERX_REDACT_FIELDS", "").split(",")
        if part.strip()
    )
    return SENSITIVE_LABEL_PARTS + extra


def _placeholders_in_commands(commands: list[CDPCommand]) -> list[str]:
    found: set[str] = set()
    for command in commands:
        for match in PLACEHOLDER_RE.finditer(json.dumps(command.params, sort_keys=True)):
            found.add(match.group(1))
    return sorted(found)


def _redacted_fields_in_commands(commands: list[CDPCommand]) -> list[str]:
    fields: set[str] = set()
    for command in commands:
        if not command.metadata.get("redacted"):
            continue
        placeholder = str(command.metadata.get("placeholder", ""))
        match = PLACEHOLDER_RE.fullmatch(placeholder)
        fields.add(match.group(1) if match else placeholder)
    return sorted(fields)


# Short values such as "1" or "en" collide with ordinary path segments, so
# only longer values are turned into URL placeholders automatically. An
# explicit ``{{name}}`` in a URL always works.
_MIN_AUTO_URL_VALUE_LENGTH = 3


def _templatize_url(url: str, variables: dict[str, Any]) -> tuple[str, list[str]]:
    """Replace whole path segments equal to a variable value with placeholders.

    Returns the URL template and any problems that make it unsafe to replay.
    A segment matching two variables is ambiguous and is not guessed.
    """
    parsed = urlparse(url)
    segments = parsed.path.split("/")
    problems: list[str] = []
    for index, segment in enumerate(segments):
        if not segment or PLACEHOLDER_RE.fullmatch(segment):
            continue
        decoded = unquote(segment)
        names = [
            name
            for name, value in variables.items()
            if len(str(value)) >= _MIN_AUTO_URL_VALUE_LENGTH and decoded == str(value)
        ]
        if len(names) > 1:
            problems.append(
                "a URL path segment matches more than one variable: " + ", ".join(sorted(names))
            )
        elif names:
            segments[index] = f"{{{{{names[0]}}}}}"
    return urlunparse(parsed._replace(path="/".join(segments))), problems


def _interpolate_url(template: str, variables: dict[str, Any]) -> str:
    """Fill URL path placeholders, percent-encoding each value as one segment."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise MissingReplayVariable(name)
        return quote(str(variables[name]), safe="")

    return PLACEHOLDER_RE.sub(replace, template)


def _interpolate_placeholders(value: str, variables: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise MissingReplayVariable(name)
        return str(variables[name])

    return PLACEHOLDER_RE.sub(replace, value)
