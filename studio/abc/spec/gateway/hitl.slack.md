# fiber.studio.abc.spec.gateway.hitl.slack
@desc: Out-of-Band (OOB) HITL Bridge, State Envelope & Slack Socket Mode Specification

## 1. Cryptographic State Envelope & OOB Gate Architecture

When `MCPServerConnector` parks a high-risk call (`HTTP 202`), `OutOfBandApprovalBridge` and `SlackHitlBridge` coordinate human attestation without relying on a centralized approval database.

```text
[MCP_PHASE_YIELD Event] ──► [SlackHitlBridge]
                              └─ Mint Ed25519-Signed `ApprovalStateEnvelope` & Post Block-Kit Card
                                   │
                                   ▼ (Operator Clicks Approve / Deny in Slack)
[OutOfBandApprovalBridge.handle_slack_interaction]
  ├─ Gate 1 (Transport): Verify Slack `v0=` HMAC-SHA256 & 300s replay drift window
  ├─ Gate 2 (Envelope):  Verify NodeSigner Ed25519 signature & `exp` TTL on `ApprovalStateEnvelope`
  ├─ Gate 3 (Replay):    Consume signature in single-use `_consumed_tokens` cache
  └─ Gate 4 (Identity):  Map external user -> SPIFFE ID & publish `McpIntentDispatch` (`AttestedResumeResult`)
       │
       ▼
[ValidatorService & PreflightGuard.verify_and_pop]
  └─ Decrypt Admin AES-GCM Vault -> Verify 6-Digit TOTP (or 1-Click SPIFFE) -> Release Stashed Payload

```

### 1.1. Wire Contracts (`fiber.gateway.bridge.channel.oob`)

| Contract Model | Core Fields | Optional Extension Slots | Security Role |
| --- | --- | --- | --- |
| **`ApprovalStateEnvelope`** | `handle_id`, `target_id`, `prompt_id`, `exp` | `node_fp`, `bridge_id`, `require_totp`, `extra` | Embedded in Slack button values; Canonical JSON signed with node `Ed25519` key to prevent handle hijacking and cross-node collisions (`node_fp`). |
| **`AttestedResumeResult`** | `value` (OTP), `attested_user_id` (SPIFFE) | `override_args`, `lease_ttl_sec`, `quorum_sigs`, `comment` | Injected into `mcp.intent.queue.<target_id>` upon verification; omits `None` fields on the wire for 100% backward compatibility. |
| **`McpIntentDispatch`** | `handle_id`, `action` (`RESUME` | `FORCE_ROLLBACK`) | `prompt_id`, `result`, `reason` | Unfreezes parked worker on `APPROVE` or immediately aborts/reverts transaction on `DENY`. |

### 1.2. Attestation Modes (`TOTP` vs. `1-Click Button`)

| Mode | Trigger Syntax | Slack Block-Kit UI | Validator Check (`ValidatorService`) |
| --- | --- | --- | --- |
| **6-Digit TOTP** *(Default)* | `"RESTRICTED_TOPIC|DROP"` | Renders `fiber_totp_block` (`plain_text_input`) + **Approve / Deny** buttons. | Slack HMAC + Ed25519 Envelope + AES-GCM Vault Decrypt + **RFC-6238 6-Digit OTP Match** + Payload Hash Signature. |
| **1-Click Button** | `"button:prod_secrets|DELETE"` | Omits TOTP input; renders **Approve & Resume** / **Deny & Rollback** buttons only. | Slack HMAC + Ed25519 Envelope + AES-GCM Vault Existence + **Authorized SPIFFE Mapping** + Payload Hash Signature. |

---

## 2. Node Keychain & Admin Vault Provisioning (`fiber sign`)

Both TOTP and 1-Click modes require the operator's mapped SPIFFE ID to be enrolled in the local AES-GCM SQLite vault (`deploy_audit.sqlite`):

```bash
$ fiber sign init                          # Initialize Ed25519 node identity & local_cipher.key
$ fiber sign enroll -u spiffe://self/fiber # Enroll admin SPIFFE ID & render terminal TOTP QR code
$ fiber sign verify -u spiffe://self/fiber # Test AES-GCM decryption & 6-digit RFC-6238 code
```

---

## 3. Slack Socket Mode Setup

`SlackHitlBridge` operates over **100% Outbound Socket Mode** (zero inbound firewall ports required) and replaces interactive cards in-place (`replace_original: True`) with an immutable **Zero-Trust Attestation Receipt** upon resolution.

### 3.1. 1-Click App Provisioning via Manifest CLI

Instead of configuring Socket Mode, Interactivity, and OAuth scopes manually, use the built-in manifest provisioner (`fiber.node.ops.channel.slack`), which pre-configures `socket_mode_enabled: true`, `interactivity: true`, and required bot scopes (`chat:write`, `chat:write.public`, `channels:read`, `channels:join`):

```bash
# Print 3-step setup guide & open pre-populated Slack App Manifest URL in browser (or `json` for raw manifest)
$ fiber ops channel.slack guide --open
```

1. **Create App:** Select your workspace in the opened browser tab and click **Create**.
2. **App-Level Token (`xapp-...`):** Navigate to **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**, add `connections:write`, and copy `xapp-1-...`.
3. **Bot Token (`xoxb-...`):** Navigate to **Install App** → **Install to Workspace** and copy `xoxb-...`.

### 3.2. Environment Variables (`.env`)

| Variable | Required | Description |
| --- | --- | --- |
| `FIBER_SLACK_BOT_TOKEN` | **Yes** (Live) | Bot User OAuth Token (`xoxb-...`) for posting Block-Kit prompts. |
| `FIBER_SLACK_APP_TOKEN` | **Yes** (Socket) | App-Level Token (`xapp-...`) for outbound Socket Mode WebSocket. |
| `FIBER_SLACK_CHANNEL_ID` | **Yes** (Live) | Target Slack Channel ID (e.g., `C0A9L79HY57`). Auto-discovered during interactive `tester` runs. |
| `FIBER_SLACK_USER_ID` | Optional | Slack User ID mapped to enrolled SPIFFE ID (`*` fallback supported in dev). |
| `FIBER_SLACK_SIGNING_SECRET` | Optional | Webhook HMAC secret *(auto-derived from node `Ed25519` pubkey if omitted)*. |

---

## 4. Verification & E2E Testing

### 4.1. Isolated Channel & Token Smoke Test (`ops.channel.slack`)

Verifies Slack Socket Mode connectivity, auto-corrects swapped `xoxb`/`xapp` tokens, interactively discovers and joins public channels (`conversations.list` / `conversations.join`), and validates local RFC-6238 TOTP against `deploy_audit.sqlite` without booting full MCP workers:

```bash
# Interactive 1-Click Button round-trip test (auto-discovers & joins channel if --channel is omitted)
$ fiber ops channel.slack test --bot-token "xoxb-..." --app-token "xapp-..."

# Interactive 6-Digit TOTP verification round-trip test
$ fiber ops channel.slack test --bot-token "xoxb-..." --app-token "xapp-..." --totp

```

### 4.2. Full Gateway Pre-Flight E2E Suite (`hitl.slack`)

Validates the complete MCP Gateway pipeline across both **Live Slack Socket Mode** (when `FIBER_SLACK_BOT_TOKEN` and `FIBER_SLACK_APP_TOKEN` are present) and **Local Webhook Emulation** (offline CI/CD):

```bash
# Phase 1 (Dual Guard Arming) -> Phase 2 (Safe 200 OK) -> Phase 3 (Network TOTP) -> Phase 4 (Stdio Zero-Spawn 1-Click)
$ fiber e2e gateway.hitl.slack
```