# TERX documentation

TERX is a local, policy-enforced replay gate for repeatable browser-agent
workflows. It reuses only workflows whose caller scope, starting conditions,
semantic targets, and outcome proof match the current run.

## Guides

| Guide | Description |
| --- | --- |
| [Quick start](quickstart.md) | Python and MCP replay contracts |
| [Integrations](integrations.md) | Lightweight MCP and Python workflow adapter |
| [Developer guide](development.md) | Semantic IR, policy boundary, and tests |
| [Benchmarks](benchmarks.md) | Historical benchmark methodology and limits |
| [Project structure](project-structure.md) | Repository layout |
| [Stagehand](stagehand.md) | Experimental integration notes |
| [Changelog](changelog.md) | Version history |

## v0.4 boundary

TERX stores only labelled navigation, click, and named-variable text actions.
It refuses raw JavaScript, coordinate input, ambiguous targets, failed
conditions, wrong scope, and destructive hits without per-run approval. See the
[quick start](quickstart.md) for the supported contract and
[SECURITY.md](../SECURITY.md) for data and egress boundaries.

## Verify

```bash
pytest tests/ -v
ruff check .
terx eval-local
```

The local eval verifies TERX's supported headless-Chrome path; it does not
claim universal site or agent compatibility.
