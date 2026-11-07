# fiber.dev.abc.spec.infra.mcp.connector
@desc: MCP Connector Transport, Pre-Flight Guard & In-Band Protocol Specification

## 1. Connector Transport & Interception Matrix

`MCPServerConnector` governs tool execution across local and remote MCP servers, enforcing deterministic idempotency and pre-flight interception before worker I/O or subprocess creation.

| Transport / Mode | Target Target Type | Interception Guarantee (`HTTP 202 YIELD`) | Lifecycle & Session |
| :--- | :--- | :--- | :--- |
| **`network` / `multiplex`** | Remote HTTP / Streamable SSE (`/mcp`, `/sse-mcp`) | **Pre-I/O Stashing:** Traps matching payloads prior to upstream HTTP/SSE dispatch. | Persists `Mcp-Session-Id` across concurrent multiplexed streams; maps upstream `401` or JSON-RPC faults to Gateway `502`. |
| **`stdio` / `ephemeral`** | 3rd-Party CLI / `npx` packages | **Zero-Spawn Isolation:** OS subprocess (`PID`) is never spawned until attestation succeeds (`active_sandboxes=0`). | Lazy-spawns sandbox on approval and terminates immediately upon resolution. |
| **`stdio` / `linear`** | Built-in Stateful Daemons (`worker.deployer`, `finlib`) | **Pre-I/O or Native Elicitation:** Traps via regex or mid-flight worker `elicitation/createMessage`. | Serialized queue processing; unapproved orphan streams are force-reverted by `WorkerReaper` (`Sentinel`). |

---

## 2. Pre-Flight Guard Provisioning (`PreflightGuard`)

`PreflightGuard` inspects incoming JSON-RPC `params` against regular expressions. Prefixing a pattern with `button:` (or `simple:`, `nototp:`, `1click:`) switches attestation from default **6-Digit TOTP** to **1-Click Button** mode.

### 2.1. Resolution Precedence
`PreflightGuard.resolve_pattern()` evaluates rules in strict priority order:
1. **Explicit CLI / SDK Flag:** `-p` / `--preflight` or `preflight_pattern="..."`
2. **Target-Scoped Env:** `FIBER_PREFLIGHT_<SANITIZED_TARGET_ID>` *(e.g., `hitl-stdio-fs` -> `FIBER_PREFLIGHT_HITL_STDIO_FS`)*
3. **Global Env:** `FIBER_PREFLIGHT_PATTERN` or `FIBER_HITL_ON`
*(Mode overrides follow: `button:` prefix -> `FIBER_PREFLIGHT_MODE_<TARGET>` -> `FIBER_PREFLIGHT_MODE`).*

### 2.2. CLI & Declarative `.mcp.json` Mounting
`McpConfigMapper` extracts and strips `FIBER_PREFLIGHT_*` keys from `.mcp.json` prior to worker spawn so internal flags never leak into 3rd-party environments:

```bash
# CLI Sidecar Provisioning (TOTP on Postgres, 1-Click Button on Filesystem, Env-Auth on Remote HTTP)
$ fiber connect -t prod-db -e "npx -y @modelcontextprotocol/server-postgres" -p "DROP|TRUNCATE|DELETE"
$ fiber connect -t local-fs -e "npx -y @modelcontextprotocol/server-filesystem /tmp" -p "button:prod_secrets|\.env"
$ fiber connect -t ctx7-remote -e https://mcp.context7.com/mcp -p "RESTRICTED_TOPIC" -f .env

```

```json
{
  "mcpServers": {
    "fiber-gateway": { "url": "http://127.0.0.1:8356/v1/mcp-gateway/invoke" },
    "workspace-fs": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {
        "FIBER_PREFLIGHT_PATTERN": "prod_secrets|DELETE",
        "FIBER_PREFLIGHT_MODE": "button"
      }
    }
  }
}
```

---

## 3. Invocation, Parking (`HTTP 202`) & Resumption Protocol

```text
[Client / Agent] ──(POST /v1/mcp-gateway/{target}/invoke + x-idempotency-key)──► [MCPServerConnector]
  ├─ Safe Payload      ──► Execute Transport (HTTP/SSE/Stdio) ──► Return HTTP 200 OK
  └─ Trap / Elicitation ──► Stash Payload (SHA-256) & Seal PENDING/YIELD
                        ──► Return HTTP 202 Accepted (Location: /.../handles/{handle_id})
                              ├─ [Path A: In-Band Resume]  Re-POST with `_meta.inputResponses` (AttestedResumeResult)
                              ├─ [Path B: OOB Resume]      Slack/OOB Bridge dispatches signed RESUME intent
                              └─ [Path C: Timeout Expiry]  Sentinel (WorkerReaper) dispatches FORCE_ROLLBACK -> FAULTED

```

### 3.1. `HTTP 202 Accepted` Yield Frame

Duplicate requests with the same `x-idempotency-key` hit the Idempotency Fast-Path and return the parked `202` state without re-triggering the worker:

```json
{
  "jsonrpc": "2.0",
  "id": "prompt_txn_1792523154_ad45a9be4329",
  "method": "elicitation/createMessage",
  "params": {
    "message": "[Pre-flight Guard] High-risk pattern 'RESTRICTED_TOPIC' detected on 'net-context7'. Enter TOTP:",
    "payload_hash": "1099bd1e...",
    "require_totp": true
  }
}

```

### 3.2. In-Band Stateless Resumption (`_meta.inputResponses`)

Agents capable of collecting credentials directly can resume a parked transaction in-band by re-submitting the original payload with the **same `x-idempotency-key**` and an embedded `AttestedResumeResult`:

```json
{
  "jsonrpc": "2.0",
  "id": 777,
  "method": "tools/call",
  "params": {
    "name": "execute_db_migration",
    "arguments": { "service_name": "auth", "sql_script": "DROP TABLE" },
    "_meta": {
      "inputResponses": {
        "jsonrpc": "2.0",
        "id": "prompt_txn_1792523154_ad45a9be4329",
        "result": { "value": "482910" }
      }
    }
  }
}
```

---

## 4. Verification (`worker.protocol`)

```bash
# Validates HTTP Multiplexing, SSE framing, 401/502 translation, In-Band TOTP Resume & Sentinel Reaper rollback:
$ python -m fiber.dev.e2e.gateway.worker.protocol
```