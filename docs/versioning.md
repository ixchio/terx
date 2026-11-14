# Versioning and deprecation policy

TERX follows [Semantic Versioning](https://semver.org/) from 1.0.0 onward:
`MAJOR.MINOR.PATCH`.

- **PATCH** releases fix bugs without changing any public contract.
- **MINOR** releases add features in a backward-compatible way and may
  deprecate (never remove) public API.
- **MAJOR** releases may remove deprecated API or change public contracts.

Before 1.0.0, minor releases could break compatibility. Releases in the 0.x
line are not supported after 1.0.0 ships.

## What counts as public

The compatibility promise covers:

| Surface | Public contract |
| --- | --- |
| Python | Names in `terx.__all__` and `terx.cache.__all__`, `terx.integrations.workflow`, and `terx.tools.SavedTool`, with their documented parameters and return types |
| Legacy import path | `terx.cache.cache` re-exports every name it defined in 0.5.x, for all of 1.x |
| MCP | Tool names, parameter names, and documented result keys of `terx-server` |
| CLI | `terx` subcommands, their flags, and their JSON output keys |
| Cache data | The SQLite schema version and the semantic action format in `commands_json` |
| Replay contract | Condition keys, side-effect classes, and the fields of `ReplayApprovalRequest` and `ApprovalDecision` |

Names that start with an underscore, and module internals not listed above,
are private. They can change in any release, even when they are re-exported
by the legacy `terx.cache.cache` module.

## Security and fail-closed changes

A change that makes TERX refuse something it previously allowed, for example a
newly blocked URL scheme or a stricter condition check, can ship in a MINOR or
PATCH release when it fixes a security or correctness problem. Such a change
always fails closed (TERX refuses rather than acting) and is listed under
**Security** or **Fixed** in the changelog, with the old behavior described.

TERX never loosens a safety check in a MINOR or PATCH release.

## Deprecation process

1. The old behavior keeps working and emits a `DeprecationWarning` that names
   the replacement and the release that will remove it.
2. The changelog lists it under **Deprecated**.
3. It stays for at least one MINOR release and at least three months.
4. It is removed only in the next MAJOR release, listed under **Removed**.

## Cache schema migrations

- `MemoryCache` migrates an older cache database forward on first open.
  Migrations never delete a user's workflows except rows that can no longer be
  replayed safely, and those are kept for inspection where possible.
- Opening a cache with an older TERX release than the one that migrated it is
  not supported. Back up `.terx/` before downgrading.
- A schema change that requires re-recording workflows is a MAJOR change.

## Supported Python versions

TERX supports each CPython version that is not end-of-life and that CI tests
(currently 3.11, 3.12, and 3.13). Dropping a Python version happens in a MINOR
release, announced at least one release ahead in the changelog.
