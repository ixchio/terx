"""Optional integration surfaces.

Nothing here starts a browser, worker, or background service at import time.
Import only the adapter the host application needs.
"""

from terx.integrations.browser_use import (
    BrowserUseRunResult,
    TerxBrowserUseAdapter,
    wrap_browser_use,
)
from terx.integrations.workflow import TerxActions, TerxWorkflow, WorkflowRunResult

__all__ = [
    "BrowserUseRunResult",
    "TerxActions",
    "TerxBrowserUseAdapter",
    "TerxWorkflow",
    "WorkflowRunResult",
    "wrap_browser_use",
]
