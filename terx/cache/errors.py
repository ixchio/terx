"""Exceptions raised by TERX replay and recording."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


class CacheReplayError(Exception):
    """Raised when a cached CDP command fails during replay (DOM drift)."""

    def __init__(self, failed_method: str) -> None:
        self.failed_method = failed_method
        super().__init__(f"Replay failed at CDP method: {failed_method}")


class ReplayRefused(Exception):
    """Raised when TERX deliberately declines an unsafe or ambiguous replay."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Replay refused: {reason}")


class MissingReplayVariable(Exception):
    """Raised when replay needs a `{{variable}}` value that was not supplied."""

    def __init__(self, variable_name: str) -> None:
        self.variable_name = variable_name
        super().__init__(f"Replay requires variable: {variable_name}")


class PostconditionFailed(Exception):
    """Raised when replay completes but the expected page state is not reached."""


class MutationDriftError(Exception):
    """Raised when replay causes unusually high DOM churn."""

    def __init__(self, count: int, threshold: int) -> None:
        self.count = count
        self.threshold = threshold
        super().__init__(
            f"Replay mutation drift detected: {count} DOM changes exceeded threshold {threshold}"
        )
