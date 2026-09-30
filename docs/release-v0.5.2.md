# TERX v0.5.2 release

Released 2026-10-01.

This is a release-engineering patch for v0.5.0's saved-tool feature release.
It pins Ruff to `0.15.11` in CI and the development extra, formats the remaining
project files with that exact version, and pins MCP to the compatible 1.x API.

The previous workflow installed an unpinned Ruff release, whose new default
checks failed CI before tests ran. It also accepted MCP 2.x, where the FastMCP
server API used by TERX has been removed. The supported dependency is now
`mcp>=1.0,<2` until TERX intentionally migrates its MCP server.

No replay policy, saved-tool contract, or public compatibility claim changed.
See the [v0.5.0 feature release](release-v0.5.0.md) for the shipped capability,
local Chrome proof, and honest Browser Use cost observations.

## Verification

```bash
ruff format --check .
ruff check .
pytest tests/ -q
python3 -m terx.evals.local_suite
```
