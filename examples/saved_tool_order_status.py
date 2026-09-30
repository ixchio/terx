"""Run TERX's reproducible saved-tool order-status example.

This starts a temporary local vendor page and Chrome, records the supported
workflow, saves ``check_order_status``, reconnects the CDP session, then calls
it with a new account and order ID. No credential, cloud browser, or model key
is required.
"""

from __future__ import annotations

import asyncio
import json

from terx.evals.local_suite import run_suite


async def main() -> None:
    report = await run_suite()
    order_status = next(
        case for case in report["cases"] if case["task"] == "check local vendor order status"
    )
    print(json.dumps(order_status, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
