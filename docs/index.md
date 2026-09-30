# TERX documentation

TERX is a local, policy-enforced replay gate for repeatable browser-agent
workflows. It reuses only workflows whose caller scope, starting conditions,
semantic targets, and outcome proof match the current run.

## Guides

| Guide | Description |
| --- | --- |
| [Quick start](quickstart.md) | Python and MCP replay contracts |
| [Integrations](integrations.md) | Lightweight MCP, saved tools, and Python workflow adapter |
| [Developer guide](development.md) | Semantic IR, policy boundary, and tests |
| [v0.5.2 release](release-v0.5.2.md) | CI dependency compatibility patch |
| [v0.5.0 release](release-v0.5.0.md) | Saved tools, fresh results, and verification |
| [v0.4.0 release](release.md) | Historical replay-contract release |
| [Benchmarks](benchmarks.md) | Historical benchmark methodology and limits |
| [Project structure](project-structure.md) | Repository layout |
| [Stagehand](stagehand.md) | Experimental integration notes |
| [Changelog](changelog.md) | Version history |

## v0.5 boundary

TERX stores only labelled navigation, click, and named-variable text actions.
It refuses raw JavaScript, coordinate input, ambiguous targets, failed
conditions, wrong scope, and destructive hits without per-run approval. v0.5
can promote an already approved record into a named MCP tool with fresh,
declarative page output; it does not add a general-purpose browser runtime. See the
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
