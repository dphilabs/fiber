# SECURITY

Fiber is a local runtime harness. It runs on your machine and in your own cloud account, and DphiLabs holds no user credentials or account access. A vulnerability in Fiber is therefore a vulnerability in infrastructure you operate yourself, so we treat reports as a priority.

## Supported Versions

| Version | Supported |
|---|---|
| Latest release (currently `v1.1.5.0`) | Yes |
| Older releases | No. Please upgrade |

Security fixes go into the latest release only.

## Reporting a Vulnerability

**Do not open a public issue, discussion, or pull request for a security problem.**

Report privately through GitHub private vulnerability reporting:
[Report a vulnerability](https://github.com/dphilabs/fiber/security/advisories/new)

Please include:

- Affected version (release tag, e.g. `v1.1.5.0`, or the commit hash) and install method (`uv pip` from git, or Homebrew)
- The component involved (see [Scope](#scope))
- Steps to reproduce. A minimal config or a VCR cassette is ideal
- What an attacker gains

**Never include real credentials, API keys, or personal data in a report.** Use placeholders or synthetic values. If a reproduction needs a secret, describe its shape, not its value.

## What to Expect

| Stage | Target |
|---|---|
| Acknowledgement | within 3 business days |
| Initial assessment and severity | within 7 days |
| Fix or mitigation plan | communicated with the assessment |
| Public disclosure | coordinated with the reporter, by default within 90 days of the report |

We will credit you in the advisory unless you ask us not to. Fixed issues are published as GitHub Security Advisories, with a CVE where applicable.

## Scope

In scope are issues in this repository that break a security property Fiber claims:

- **MCP gateway:** bypassing pre-approval (executing a tool call without the `HTTP 202` approval step), replaying or forging approvals, or breaking idempotent execution so that an action runs twice
- **LLM guardrails:** PII that leaks through masking, credentials that pass the credential blocker, or a stream that keeps running after the kill switch fires
- **VCR replay:** recorded cassettes that capture secrets despite masking, or replay that produces non-deterministic results usable to hide behavior
- **Cloudflare edge plane:** Fiber-provided Worker code or deployment templates that expose data or grant access beyond their documented scope
- **Release supply chain:** any way to publish, or get users to install, a Fiber artifact that was not built by this repository's release workflow

Out of scope:

- Misconfiguration of your own Cloudflare account, LLM provider account, or host machine
- Vulnerabilities in third-party LLM providers or MCP servers that Fiber connects to (report those upstream; tell us if Fiber makes them worse)
- Denial of service through resource exhaustion on your own machine
- Findings from automated scanners without a demonstrated impact
