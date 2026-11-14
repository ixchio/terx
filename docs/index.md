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
| [Threat model](threat-model.md) | Assets, trust boundaries, threats, and residual risks |
| [Versioning](versioning.md) | Semantic versioning, public API, and deprecation policy |
| [1.0.0 release](release-v1.0.0.md) | Stable API, six replay actions, hard eval, strict typing |
| [v0.5.3 release](release-v0.5.3.md) | CI and hosted-Chrome compatibility patch |
| [v0.5.0 release](release-v0.5.0.md) | Saved tools, fresh results, and verification |
| [v0.4.0 release](release.md) | Historical replay-contract release |
| [Benchmarks](benchmarks.md) | Historical benchmark methodology and limits |
| [Project structure](project-structure.md) | Repository layout |
| [Stagehand](stagehand.md) | Experimental integration notes |
| [Changelog](changelog.md) | Version history |

## 1.0 boundary

TERX stores only six semantic actions: navigation (with `{{variable}}` URL
path templates), click, text entry from a named variable, setting a checkbox,
choosing a dropdown option, and pressing Enter, Tab, or Escape. Targets are
matched by accessible role and name, and their recorded signals must agree.
It refuses raw JavaScript, coordinate input, ambiguous targets, failed
conditions, wrong scope, and destructive hits without per-run approval. An
approved record can be promoted into a named MCP tool with fresh, declarative
page output; TERX does not add a general-purpose browser runtime. See the
[quick start](quickstart.md) for the supported contract and
[SECURITY.md](../SECURITY.md) for data and egress boundaries.

## Verify

```bash
pytest tests/ -v
ruff check .
mypy
terx eval-local
terx eval-hard --gate
```

The local eval verifies TERX's supported headless-Chrome path; it does not
claim universal site or agent compatibility.
