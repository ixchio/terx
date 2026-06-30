# Contributing to TERX

First off — thanks for taking the time to contribute. TERX is a small, focused library and we want to keep it that way. Every PR gets read carefully.

---

## Ways to Contribute

| Type | Welcome? |
|:-----|:--------:|
| Bug fixes | ✅ Always |
| Performance improvements | ✅ Always |
| New CDP integrations / adapters | ✅ With prior discussion |
| Documentation improvements | ✅ Always |
| New features | Discuss first → open an issue |
| Refactors for style | Generally no |

---

## Getting Started

```bash
git clone https://github.com/ixchio/terx && cd terx
pip install -e ".[dev]"
pytest tests/ -v
terx demo           # sanity check — runs headless Chrome locally
terx eval-local     # deterministic local eval suite
```

> **Requirement:** Python 3.11+, Google Chrome installed, ChromeDriver not needed (raw CDP).

---

## Before You Open a PR

1. **Open an issue first** for anything non-trivial. Saves everyone time.
2. **Run the test suite**: `pytest tests/ -v` — all tests must pass.
3. **Run the local eval**: `terx eval-local` — no regressions allowed.
4. **Keep it focused**: one logical change per PR. No bundled refactors.
5. **Update docs** if you changed behavior or added a feature.

---

## Code Style

- Follow existing patterns — look at the surrounding code.
- No new runtime dependencies without a very good reason.
- Type hints everywhere (we use them for correctness, not just aesthetics).
- Docstrings for public APIs.

```bash
# Formatting
black terx/ tests/
isort terx/ tests/
```

---

## Benchmark Sensitivity

If your change touches the CDP bridge, cache, or DOM extractor — run the benchmark suite:

```bash
python -m terx.benchmarks.baseline
GROQ_API_KEY=... python -m terx.benchmarks.real_agent   # optional, needs key
```

Include before/after numbers in your PR description.

---

## Commit Messages

Keep them short and imperative:

```
fix: handle DOM nodes with missing backendNodeId
feat: add postcondition timeout option
docs: clarify variable interpolation syntax
```

---

## Opening a PR

- Target `main`
- Fill out the PR template
- Link the related issue (if any)
- Mark as Draft if it's still WIP

A maintainer will review within a few days. We aim for constructive, fast reviews.

---

## Reporting Bugs

Use the [Bug Report](.github/ISSUE_TEMPLATE/bug_report.md) template. Include:
- TERX version (`pip show terx`)
- Chrome version (`google-chrome --version`)
- Minimal repro script
- Full traceback

---

## Security Issues

**Do not** open a public issue for security vulnerabilities. See [SECURITY.md](SECURITY.md).

---

MIT License. By contributing, you agree your code will be released under the same license.
