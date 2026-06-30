# Security Policy

## Supported Versions

| Version | Supported |
|:--------|:---------:|
| Latest stable (PyPI) | ✅ |
| Older releases | ❌ |

Always run the latest version from PyPI: `pip install --upgrade terx`

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report privately via GitHub's built-in security advisory:  
**[Report a vulnerability](https://github.com/ixchio/terx/security/advisories/new)**

Or email the maintainer directly (address in the PyPI metadata).

### What to include

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

### Response timeline

- **Acknowledgement**: within 48 hours
- **Initial assessment**: within 5 business days
- **Fix + disclosure**: coordinated after patch is ready

We follow responsible disclosure. We'll credit you in the release notes unless you prefer anonymity.

## Security Scope

TERX connects to a local Chrome instance via raw CDP WebSocket. Key security boundaries:

- **URL scheme validation** — `browser_navigate` rejects `javascript:`, `data:`, `file:` schemes
- **Secret redaction** — typed values in password/token/key fields are stored as `{{placeholder}}` and never logged in plaintext
- **Local SQLite only** — the cache is a local file; no network egress of cached data
- **No remote code execution surface** — TERX does not expose an HTTP API by default

If you find a way to bypass any of these, please report it.
