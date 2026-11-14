"""Compatibility module: the original single-file TERX cache.

The implementation now lives in focused modules under ``terx.cache``
(``storage``, ``policy``, ``recording``, ``actions``, ``conditions``,
``redaction``, ``guard``, ``models``, ``errors``). Every name this module
used to define is re-exported here, so ``from terx.cache.cache import ...``
keeps working throughout 1.x. New code should import from ``terx`` or
``terx.cache``.
"""

from terx.cache.errors import (  # noqa: F401
    CacheReplayError,
    ReplayRefused,
    MissingReplayVariable,
    PostconditionFailed,
    MutationDriftError,
)
from terx.cache.policy import (  # noqa: F401
    SIDE_EFFECTS,
    REPLAY_CONDITION_KEYS,
    REPLAY_ACTIONS,
    ReplayPolicy,
    ReplayApprovalRequest,
    ApprovalDecision,
    ApprovalVerifier,
    ReplayDecision,
    _is_replayable_url,
)
from terx.cache.models import (  # noqa: F401
    CDPCommand,
    CachedSequence,
    ReplayCostLedger,
    ReplayReport,
)
from terx.cache.redaction import (  # noqa: F401
    PLACEHOLDER_RE,
    SENSITIVE_LABEL_PARTS,
    _is_sensitive_key,
    _sanitize_value,
    _sanitize_command_for_storage,
    _replay_command_digest,
    _prepare_recorded_params,
    _placeholder_for_value,
    _placeholder_for_node,
    _normalize_variable_name,
    _normalize_variables,
    _is_sensitive_input,
    _redact_all_text_enabled,
    _sensitive_label_parts,
    _placeholders_in_commands,
    _redacted_fields_in_commands,
    _interpolate_placeholders,
)
from terx.cache.conditions import (  # noqa: F401
    REPLAY_POLL_INTERVAL,
    _assert_postcondition,
    _wait_for_condition,
    _postcondition_summary,
)
from terx.cache.actions import (  # noqa: F401
    MUTATING_CDP_METHODS,
    SAFE_CLICK_FUNCTIONS,
    _SELECT_EDITABLE_CONTENTS,
    replace_field_text,
    _target_for_backend_node,
    _find_replay_target,
    _preflight_replay,
    _resolve_replay_target,
    _execute_replay_action,
)
from terx.cache.guard import (  # noqa: F401
    _start_mutation_guard,
    _read_mutation_count,
    _stop_mutation_guard,
)
from terx.cache.storage import (  # noqa: F401
    SIMILARITY_THRESHOLD,
    AUDIT_DIR,
    SCOPE_KEY_FILENAME,
    _SCOPE_DIGEST_CONTEXT,
    MemoryCache,
    _load_or_create_scope_key,
    _task_key,
)
from terx.cache.recording import (  # noqa: F401
    RecordingContext,
    session_for,
)
