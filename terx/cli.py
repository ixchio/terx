"""Command line tools for TERX operators and maintainers."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from terx import __version__
from terx.cache.cache import MemoryCache


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terx", description="TERX browser-agent memory tools")
    parser.add_argument("--db", default=".terx/cache.db", help="Path to TERX SQLite cache DB")

    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check local TERX and Chrome/CDP readiness")
    doctor.add_argument("--host", default="localhost", help="Chrome DevTools host")
    doctor.add_argument("--port", type=int, default=9222, help="Chrome DevTools port")
    doctor.add_argument("--strict", action="store_true", help="Exit non-zero when Chrome/CDP is absent")
    doctor.set_defaults(func=_cmd_doctor)

    stats = sub.add_parser("stats", help="Print cache statistics as JSON")
    stats.set_defaults(func=_cmd_stats)

    inspect_cmd = sub.add_parser("inspect", help="Inspect cached task sequences")
    inspect_cmd.add_argument("--domain", help="Filter by domain")
    inspect_cmd.add_argument("--limit", type=int, default=20, help="Maximum rows to print")
    inspect_cmd.set_defaults(func=_cmd_inspect)

    purge = sub.add_parser("purge", help="Delete cached sequences for one domain")
    purge.add_argument("domain", help="Domain to purge, for example app.example.com")
    purge.set_defaults(func=_cmd_purge)

    demo = sub.add_parser("demo", help="Run the real local browser replay demo")
    demo.set_defaults(func=_cmd_demo)

    eval_local = sub.add_parser("eval-local", help="Run the local browser replay eval suite")
    eval_local.set_defaults(func=_cmd_eval_local)

    return parser


def _cmd_doctor(args: argparse.Namespace) -> int:
    chrome = _chrome_binary()
    db_path = Path(args.db)
    cdp = _probe_cdp(args.host, args.port)
    checks = {
        "terx_version": __version__,
        "python": sys.version.split()[0],
        "chrome_binary": chrome,
        "cache_db": str(db_path),
        "cache_db_exists": db_path.exists(),
        "cdp": cdp,
    }
    print(json.dumps(checks, indent=2, sort_keys=True))
    if args.strict and (chrome is None or not cdp["reachable"]):
        return 1
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    cache = MemoryCache(db_path=args.db)
    print(json.dumps(cache.stats(), indent=2, sort_keys=True))
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    rows = _inspect_cache(Path(args.db), domain=args.domain, limit=max(args.limit, 1))
    print(json.dumps({"sequences": rows, "count": len(rows)}, indent=2, sort_keys=True))
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    cache = MemoryCache(db_path=args.db)
    deleted = cache.invalidate(args.domain)
    print(json.dumps({"domain": args.domain, "sequences_deleted": deleted}, indent=2))
    return 0


def _cmd_demo(_: argparse.Namespace) -> int:
    from terx.demo import main as demo_main

    demo_main()
    return 0


def _cmd_eval_local(_: argparse.Namespace) -> int:
    from terx.evals.local_suite import main as eval_main

    eval_main()
    return 0


def _chrome_binary() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        binary = shutil.which(name)
        if binary:
            return binary
    return None


def _probe_cdp(host: str, port: int) -> dict[str, Any]:
    url = f"http://{host}:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode())
            return {
                "reachable": True,
                "url": url,
                "browser": payload.get("Browser"),
                "protocol_version": payload.get("Protocol-Version"),
            }
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"reachable": False, "url": url, "error": str(exc)}


def _inspect_cache(db_path: Path, domain: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        query = (
            "SELECT id, domain, structural_hash, task_key, task_description, "
            "commands_json, hit_count, created_at, last_used FROM sequences"
        )
        params: list[Any] = []
        if domain:
            query += " WHERE domain = ?"
            params.append(domain)
        query += " ORDER BY last_used DESC LIMIT ?"
        params.append(limit)

        rows = []
        for row in db.execute(query, params).fetchall():
            commands = json.loads(row[5])
            rows.append(
                {
                    "id": row[0],
                    "domain": row[1],
                    "structural_hash": row[2],
                    "task_key": row[3],
                    "task_description": row[4],
                    "commands": len(commands),
                    "redacted_fields": _redacted_fields(commands),
                    "hit_count": row[6],
                    "created_at": row[7],
                    "last_used": row[8],
                }
            )
        return rows
    finally:
        db.close()


def _redacted_fields(commands: list[dict[str, Any]]) -> list[str]:
    fields = set()
    for command in commands:
        metadata = command.get("metadata", {})
        if not metadata.get("redacted"):
            continue
        placeholder = str(metadata.get("placeholder", "")).strip("{}")
        fields.add(placeholder or "unknown")
    return sorted(fields)


if __name__ == "__main__":
    raise SystemExit(main())
