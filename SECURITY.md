# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest stable release | Yes |
| Older releases | No |

Upgrade with `pip install --upgrade terx`.

## Reporting a vulnerability

Do not open a public issue. Use
[GitHub private vulnerability reporting](https://github.com/ixchio/terx/security/advisories/new)
or the maintainer address in PyPI metadata. Include impact, a minimal
reproduction, and suggested remediation if available.

## v0.4 trust boundary

TERX connects to a Chrome instance exposed through a local CDP WebSocket. CDP
has the authority of that browser profile. Use a dedicated profile for browser
automation and never expose its debugging port to an untrusted network.

TERX is a replay gate, not a sandbox. A cached workflow is eligible only when
its caller scope, route, precondition, DOM structure, workflow version, and
expiry match. Postconditions must pass before a cold run is cached and after a
warm replay completes. Destructive replays require a host approval verifier to
atomically validate and consume an opaque approval token. TERX rejects any
decision that is not both approved and consumed; a non-empty token alone has no
authority.

Only `TERX.navigate`, `TERX.click`, and `TERX.type` are persisted. Arbitrary
CDP commands, raw keyboard and mouse events, coordinate clicks, and arbitrary
JavaScript are not replayable. Ambiguous or missing semantic targets are
refused, not guessed. Replayable navigation excludes query strings, fragments,
and embedded credentials so those values do not become cached workflow data.

Replay policy conditions allow only non-empty URL, title, visible-text, and
selector checks. Arbitrary JavaScript conditions cannot enter the replay cache. The
complete contract is checked with a fingerprint; condition values are not
persisted in the cache's policy summary.

## Data handling

- Cache and audit data are written locally under `.terx/` by default. Treat
  this directory as sensitive application data.
- Cached CDP response payloads are discarded. Audit records use the same
  semantic, redacted action representation as the cache.
- Typed values must be supplied through named `variables` to be cacheable.
  TERX stores placeholders such as `{{email}}`, not those variable values.
- Password, token, key, card, and other configured sensitive field names are
  redacted at persistence and audit boundaries. `TERX_REDACT_FIELDS` extends
  this label policy.
- Scope IDs are stored as a SHA-256 digest, not plaintext. A digest is an
  identifier, not a substitute for a secret; use an opaque, high-entropy scope
  ID if disclosure of the identifier matters.

## Network egress

Normal cache recording and replay make no network calls beyond the CDP browser
traffic already initiated by the workflow. The optional `SelfHealer` is not
called automatically by TERX replay and is disabled by default. If a developer
sets `TERX_ENABLE_EXPERIMENTAL_HEALING=1` and invokes it directly, it sends the
provided task, parameters, and sanitized DOM diagnostic data to the configured
LiteLLM provider. Do not enable that experimental feature for sensitive data
without reviewing the provider and the supplied inputs.

## Response targets

We aim to acknowledge reports within 48 hours and provide an initial assessment
within five business days. Disclosure is coordinated after a fix is available.
