# fiber.xyz.spec.worker.builtin
@desc: Built-in MCP Workers Spec

> **Related:** [MCP Connector Spec](../gateway/mcp.connector.md) · [Slack HITL Spec](../gateway/hitl.slack.md)
> **Validation:** `fiber-dev e2e worker.validation` — [Validation Spec §2.7](../validation.md#27-built-in-workers) · [Log](../../log/worker/builtin.20261127.log)

Each built-in worker subclasses `AsyncWorkerProtocol`, exposes its tools through `@tool`, and runs as a resident process via `serve_forever_async()`. Workers are registered in `~/.anchor/mcp.json` and supervised under the Local MCP Gateway (`fiber daemon`, `127.0.0.1:8356`).

| Worker | `agent_name` | Module | Role | Tools |
| :--- | :--- | :--- | :--- | :--- |
| **Duck** | `worker.duck` | `fiber.node.worker.duck` | Read-only SQL over local tabular files (DuckDB) | `describe_data`, `query_sql` |
| **Reader** | `worker.reader` | `fiber.node.worker.reader` | Section-level document index, scoped retrieval, table comparison | `sync_corpus`, `list_modules`, `retrieve_scoped`, `read_exact_section`, `changed_sections`, `compare_table` |
| **Pin** | `worker.memory.pin` | `fiber.node.worker.memory.pin` | Pinned constraints and decisions with live-verified source refs | `set`, `drop`, `resolve`, `list`, `scope` |
| **Verifier** | `worker.verifier` | `fiber.node.worker.dev.verifier` | Rule-driven checks on payloads, HTTP endpoints, and local binaries | `list_rules`, `verify_payload`, `verify_http`, `verify_binary` |
| **Mailbox** | `worker.local.mailbox` | `fiber.node.worker.daily.mailbox` | Read-only search over downloaded mail archives (`.eml` / `.mbox`) | `search_mail`, `read_mail` |
| **Media** | `worker.media` | `fiber.node.worker.daily.media` | Inspect, trim, and convert audio/video files (ffmpeg) | `probe`, `trim`, `convert` |
| **GitHistory** | `worker.git.history` | `fiber.node.worker.dev.git.history` | Commit search, blame, and patch view over the workspace repository | `search_commits`, `blame`, `show_commit` |

Pin refs can be sections, queries, symbols, files, or revisions. `resolve` and `scope` check them live against the docs, data, and code on disk.

**Errors.** Invalid input is returned as a structured tool execution error (`ToolError` → `result.isError = true`, HTTP 200) rather than an upstream gateway fault (`HTTP 502`). This covers bad arguments, paths or symlinks escaping the workspace root, missing files, and malformed values—allowing the calling agent to read the error message and self-correct its arguments.

---

## 1. Runtime

- **Registration.** `fiber connect` writes workers into `~/.anchor/mcp.json` (for example `fiber connect -t <target_id> -e "<command>" -p "<pattern>"`, or `fiber connect builtin`). `--standalone` runs a worker in the foreground without writing to `~/.anchor/mcp.json`.
- **Resident Multiplexing & Auto-Respawn.** Unlike third-party `stdio` tools that default to per-request `ephemeral` sandboxes, built-in `AsyncWorkerProtocol` workers run in **`multiplex` mode** over a single resident `stdio` pipe—correlating concurrent requests by JSON-RPC `id` while serializing pipe writes. If a resident process exits unexpectedly, `MCPServerConnector` fails in-flight futures immediately and respawns the worker on the next call.
- **Supervision.** `fiber daemon` spawns and supervises the registered workers and hot-reloads them when `~/.anchor/mcp.json` changes.
- **Invocation & Namespacing.** Agents call tools through the gateway via per-target routes (`/v1/mcp-gateway/{target_id}/invoke`, `/v1/mcp-gateway/{target_id}/tools`) or unified routes (`/v1/mcp-gateway/invoke`, `/v1/mcp-gateway/tools`). On unified routes, tool names are namespaced with `__` as `<target_id>__<tool_name>` (e.g., `worker.duck__query_sql`).
- **Idempotency.** Every invocation maps to a deterministic transaction handle (`x-idempotency-key`) in the local state ledger. Duplicate retries return the cached resolution without re-running the tool; upstream timeouts broadcast `FORCE_ROLLBACK`.
- **Pre-Flight Approval (HITL).** Calls matching a worker's pre-flight pattern (`-p` / `FIBER_PREFLIGHT_PATTERN`) are parked with `HTTP 202` (`YIELD`) at the connector boundary before any worker I/O occurs. Execution resumes only after human attestation via 6-digit TOTP (default) or 1-Click buttons (`button:` prefix) over Slack, Discord, CLI, or Console Studio. This enforces the user approval required by mutating tools such as `pin.set` and `pin.drop`.
- **Execution Plane.** `XPHI_WASM_PLANE` selects where sandboxed compute runs: `local` (default), `flare` (local Dual V8 isolates), or `flare:<url>` (self-hosted Cloudflare Workers).

---

## 2. Launch & Configuration

| Worker | CLI arguments | Defaults |
| :--- | :--- | :--- |
| **Duck** | `--root`, `--max-rows` | `.`, `200` |
| **Reader** | `--root`, `--db` | `.`, `~/.anchor/memory/reader/<sha256(root)[:12]>.db`; runtime options from `ReaderRuntimeOpts.from_daemon_env()` |
| **Pin** | `--root`, `--account`, `--db` | `.`, store identity from `MemoryIdentity.bind(account, db_path)` |
| **Verifier** | `--root`, `--rules` | `.`, `FIBER_VERIFY_RULES_PATH` or `rules.json` next to `fiber/node/bound/verify/server.py` |
| **Mailbox** | `--root` | `.` |
| **Media** | `--root`, `--timeout` | `.`, `900` (encode/trim timeout in seconds, clamped to `>= 1`) |
| **GitHistory** | `--root` | `.` |

**Host binary dependencies:**
- **Media:** `__deps__ = {"bins": ["ffmpeg", "ffprobe"]}`
- **GitHistory:** `__deps__ = {"bins": ["git"]}`

---

## 3. Duck Worker (`worker.duck`)

Queries run through a stateless `TabularSandbox(root)` (`fiber.node.bound.memory.sandbox`).
**Supported formats:** `.csv`, `.tsv`, `.json`, `.jsonl`, `.ndjson`, `.parquet`, `.xlsx`.

**Isolation & Execution:** Each call runs in an isolated in-memory session:
1. A fresh `duckdb.connect(":memory:")` connection is opened per call (`to_thread`), so concurrent multiplexed queries never share a connection. Extension auto-install and auto-load are disabled.
2. File references in `read_*('…')`, `FROM` / `JOIN '…'`, and `DESCRIBE` / `SUMMARIZE '…'` are resolved and verified to stay inside `--root` (including glob targets), then pre-mounted as temporary tables (`src_0`, `src_1`, …).
3. `SET enable_external_access = false` is enforced before executing the rewritten SQL.

As a result, queries cannot access paths outside `--root`, load extensions, or run mutating/egress statements (`COPY`, `ATTACH`, `CREATE`, `INSERT`, `UPDATE`, `DELETE`, `DROP`, `PRAGMA`, `SET`, etc.). Path-traversal checks inspect path tokens rather than arbitrary substrings, so string literals containing ellipses (e.g., `'wait...now'`) are allowed.

**Format notes:**
- `query_sql` supports glob patterns (e.g., `'logs/*.jsonl'`) as long as all matched files share one extension.
- `.xlsx` files are parsed directly from worksheet XML without external spreadsheet libraries (single file per table reference; all columns are mounted as `VARCHAR`, so numeric aggregations require explicit `CAST`).
- `NaN`/`Inf` floats serialize as `null`, `Decimal` as string, and date/time values as ISO-8601 strings.

### 3.1 `describe_data(source)`
Returns the column schema of a single local tabular file (glob patterns are rejected).
```jsonc
{
  "source": "logs/access.jsonl",
  "columns": [
    { "column_name": "service", "column_type": "VARCHAR", "null": "YES", "key": null, "default": null, "extra": null }
  ]
}
```

### 3.2 `query_sql(sql, limit=50)`
Executes a read-only analytical query (`SELECT`, `WITH`, `DESCRIBE`, `SUMMARIZE`, or `EXPLAIN`). `SELECT` and `WITH` queries are wrapped with `LIMIT` clamped to `[1, max_rows]`.
```jsonc
{ "row_count": 12, "limit": 50, "rows": [ { "service": "gateway", "latency_ms": 12 } ] }
```

---

## 4. Reader Worker (`worker.reader`)

Reader maintains an incremental SQLite section index (`CorpusStore`) per workspace root. Sections are addressed as `<doc_path>#<section_id>` (a leading `#` on `section_id` is stripped automatically). Paths resolving outside `--root` or missing on disk raise `ToolError`.

| Tool | Arguments (defaults) | Behavior |
| :--- | :--- | :--- |
| `sync_corpus` | `subpath=""` | Incrementally indexes documents under `--root` (or `subpath`) and detects stale entries. Serialized under `self._lock`. |
| `list_modules` | `subpath=""` | Returns folder-grouped document lists and H1–H3 section outlines without body text. |
| `retrieve_scoped` | `query`, `scope_prefix=""`, `cross_module=False`, `max_chars=4000`, `latest_only=True` | Returns matching sections within `scope_prefix` and character budget, plus a `MANIFEST`. `max_chars` is clamped to `[200, 50000]`. `cross_module=True` allows matches outside `scope_prefix` (triggering full-root sync if needed). `latest_only=True` excludes superseded versions. Empty `query` raises `ToolError`. |
| `read_exact_section` | `doc_path`, `section_id`, `expect_hash=""` | Reads raw section text. Flags `STALE` if `expect_hash` is provided and differs from the current hash. Empty `section_id` or unknown section raises `ToolError`. |
| `changed_sections` | `since="7d"` | Lists section addresses whose content hash changed within the window (`Nd`, `Nh`, or `YYYY-MM-DD`). |
| `compare_table` | `doc_path`, `section_id`, `source`, `keys`, `tolerance=0.0`, `table_index=0` | Joins and diffs a Markdown/document table in `section_id` against a tabular file (`.csv`, `.xlsx`, `.jsonl`, `.parquet`) on `keys` (at least one non-empty key required; `tolerance >= 0`, `table_index >= 0`). |

**Scope-aware synchronization:**
- Reader tracks whether a full workspace sync (`_full_synced`) or specific directory prefixes (`_synced_prefixes`) have completed, checking `store.is_empty()` off the event loop via `asyncio.to_thread`.
- Calling `list_modules(subpath="docs/sub")` first indexes only `docs/sub`, and a subsequent `list_modules()` or `retrieve_scoped(..., cross_module=True)` automatically expands synchronization to the full workspace root so unindexed sibling folders are never missed.
- `read_exact_section` re-syncs `doc_path` whenever the file is unindexed or its disk `mtime` differs from the stored `mtime` by `>= 1 ms`.
- `compare_table` parses `doc_path` directly from disk without mutating the SQLite index.

---

## 5. Pin Worker (`worker.memory.pin`)

Pins store durable constraints, architectural decisions, and anchors keyed by `(project, key)` (`project` defaults to `"default"`). Empty `key` or empty `addrs` raises `ToolError`. During initialization, stdout is guarded so kernel crypto/identity boot logs cannot corrupt the JSON-RPC stdio stream.

| Tool | Arguments (defaults) | Returns | Behavior |
| :--- | :--- | :--- | :--- |
| `set` | `key`, `value: str \| dict`, `refs=None`, `project="default"`, `tags=""` | `{key, project, ref_count, updated_at}` | Encodes `value`/`tags`, prepares `refs` via `MemoryResolver.prepare_all`, and upserts the record. Intended for explicit user declarations or approved actions. |
| `drop` | `key`, `project="default"` | `{dropped, key, project}` | Deletes a pin and its linked references. Requires user approval. |
| `resolve` | `key`, `project="default"` | Pin object with per-ref live status | Fetches the pin and verifies every linked ref live against on-disk docs, data, and code. Raises `ToolError` if missing or on integrity failure. |
| `list` | `project=""`, `prefix=""`, `query=""`, `tag=""`, `limit=30` | `{count, items}` | Searches pins across `project` (empty = all projects). `query` runs FTS5 BM25 ranking; `prefix` filters keys; `tag` performs a case-insensitive substring match against `public_tags`. `limit` is clamped to `[1, 100]`. |
| `scope` | `addrs: list[str]`, `project="default"` | `{pins, blocked_refs, base_rev, blocked}` | Normalizes target addresses, reverse-lookups referencing pins, and resolves each ref live. Any ref with `status != OK` is collected into `blocked_refs` (`blocked: true`). |

**`value` forms:**
- Plain constraint string, or
- Structured anchor dict (`origin`, `premises`, `layer`, `realignments`).

**Ref kinds:** `section`, `query`, `symbol`, `file`, `revision` (e.g., `src/pay/refund.py#RefundService.apply`).
**Concurrency:** Store reads/writes and the `base_rev` HEAD lookup inside `scope` execute together under `self._lock` so resolved reference states and `base_rev` represent a consistent point-in-time snapshot.

---

## 6. Verifier Worker (`worker.verifier`)

`VerifierWorker` exposes a resident `VerificationServer` (`fiber.node.bound.verify.server`). Domain failures (`VerificationError`) and malformed inputs are raised as `ToolError`. `close()` cleanly closes the underlying `httpx.AsyncClient`.

### 6.1 Rules registry (`rules.json`)
Loaded from `--rules`, `FIBER_VERIFY_RULES_PATH`, or the default `rules.json` and compiled once at startup into `VerifyRulesRegistry`. Missing files, schema errors, or invalid JMESPath expressions fail fast at startup with `VerificationError`.

| Section | Spec model | Fields used |
| :--- | :--- | :--- |
| `payload_rules` | `PayloadRuleSpec` | `description`, `jmespath_expr`, `json_schema`, `semantic_check` |
| `http_probes` | `HttpProbeSpec` | `description`, `url`, `method`, `headers`, `body`, `timeout_sec`, `expected_status`, `auth_mode`, `spiffe_id`, `jmespath_expr`, `response_schema`, `semantic_check` |
| `binary_probes` | `BinaryProbeSpec` | `description`, `executable`, `args`, `timeout_sec`, `expected_exit_codes`, `parse_stdout_json`, `jmespath_expr`, `stdout_schema`, `semantic_check` |

`semantic_check` has the shape `{ "evaluator": "<name>", "config": {...} }`.

### 6.2 Evaluation pipeline
1. **Select:** `target = jmespath(data)` if `jmespath_expr` is set; otherwise `target = data`.
2. **Schema:** Validates `target` against JSON Schema Draft 2020-12. If schema errors exist, they are returned sorted by JSON path and step 3 is skipped.
3. **Semantic:** Invokes `evaluator(target, context, config)` (sync or async). Custom evaluators can be passed to `VerificationServer(evaluators=...)` or `register_evaluator()`; `VerifierWorker` boots with an empty evaluator map by default, raising `ToolError` if a rule references an unregistered evaluator.

**Violation item:** `{ "path": "/a/b", "validator": "<keyword>", "message": "<text>" }`

### 6.3 Template parameters & injection guards
- `url` (HTTP probes) and `args` items (binary probes) bind `{name}` placeholders from `params`, with `gateway_port` defaulting to `DEFAULT_GATEWAY_PORT`.
- Missing parameters, `None` values, or values containing `\0`, `\r`, or `\n` are rejected.
- Binary probe parameters additionally reject values starting with `-` (CLI option injection) and `..` path segments (directory traversal).

### 6.4 Tools

| Tool | Arguments (defaults) | Returns |
| :--- | :--- | :--- |
| `list_rules` | — | `{rules_path, workspace_root, registered_evaluators, payload_rules, http_probes, binary_probes}` (`{id: description}` maps) |
| `verify_payload` | `rule_id`, `payload_json: str`, `context=None` | `{rule_id, passed, violation_count, violations}` |
| `verify_http` | `probe_id`, `params=None`, `body_override=None`, `context=None` | `{probe_id, url, status_code, passed, violation_count, violations}` |
| `verify_binary` | `probe_id`, `params=None`, `rel_cwd="."`, `context=None` | `{probe_id, executable, exit_code, passed, violation_count, violations, stderr_tail}` |

**`verify_http` specifics:**
- Sends `method` to the bound `url` (`follow_redirects=False`, timeout `timeout_sec`), using `body_override` when non-`None` or `spec.body` otherwise.
- Non-matching HTTP status records a `/status_code` violation and skips body checks; invalid JSON when body checks are configured records a `/body` violation.
- When `auth_mode == "fiber_dpop"`, attaches per-request `x-idempotency-key`, `x-nonce`, `x-spiffe-id`, and a 2048-bit `DPoP` proof. `X-Fiber-Fuel-Receipt` is omitted unless explicitly configured in `spec.headers` or `FIBER_FUEL_RECEIPT`.

**`verify_binary` specifics:**
- Resolves `executable` on `PATH` (`shutil.which`) and verifies `rel_cwd` resolves to a directory inside `--root`.
- Spawns the binary without a shell and with **`stdin=DEVNULL`** so child processes cannot consume messages from the worker's JSON-RPC `stdin` pipe. Kills the process on timeout.
- Non-matching exit code records `/exit_code`; invalid JSON when `parse_stdout_json=True` records `/stdout`. `stderr_tail` retains the last 500 characters of `stderr`.

---

## 7. Mailbox Worker (`worker.local.mailbox`)

Mailbox performs read-only search and retrieval over downloaded `.eml` and mbox (`.mbox`, `.mbx`, case-insensitive) archives under `--root`.

**Boundary & Symlink Safety:** Explicit `path` arguments resolving outside `--root` raise `ToolError`. During recursive directory scans (`rglob`), every candidate file is resolved (`path.resolve()`) and silently skipped if its real target lies outside `--root` (blocking symlink escapes) or is not a regular file.

**Message ID contract:**
- Single `.eml` file: `<rel>.eml` (attaching `#<index>` to `.eml` is a `ToolError`).
- mbox archive entry: `<rel>.mbox#<index>` (0-based entry index; omitting `#<index>` on an `.mbox`/`.mbx` path is a `ToolError` so an entire archive is never misparsed as one message).

**Body extraction:** Prefers `text/plain`; falls back to `text/html` stripped of `<script>`/`<style>` blocks and collapsed to plain text. Unknown or broken character encodings fall back to UTF-8 (`errors="replace"`).

### 7.1 `search_mail(query="", sender="", subject="", since="", until="", path="", limit=20)`
Searches messages and sorts matches newest first (undated messages sort last).

| Argument | Match rule |
| :--- | :--- |
| `query` | Case-insensitive substring matched against `Subject` or extracted body text |
| `sender` | Case-insensitive substring of `From` |
| `subject` | Case-insensitive substring of `Subject` |
| `since` / `until` | Strict `YYYY-MM-DD` calendar dates (inclusive, UTC). Timestamps with time/offset components or `since > until` raise `ToolError`. Header dates lacking a timezone default to UTC; unparseable dates are excluded when date bounds are active. |
| `path` | Subfolder or single archive relative to `--root` (default: whole root) |
| `limit` | Clamped to `[1, 100]` |

```jsonc
{
  "count": 3,
  "messages": [
    {
      "id": "inbox/2026.mbox#41",
      "date": "2026-11-20T09:12:00+09:00",
      "from": "…",
      "to": "…",
      "subject": "…",
      "snippet": "…",
      "attachments": ["invoice.pdf"]
    }
  ],
  "truncated": false,
  "scan_limited": false
}
```
- Scans at most `50,000` messages per call (`scan_limited: true` if capped).
- `snippet` collapses whitespace and truncates to `200` characters; unnamed attachments appear as `"(unnamed)"`.
- Zero matches raise `ToolError("No matching messages.")`.

### 7.2 `read_mail(message_id, max_chars=8000)`
Reads headers, body text (`max_chars` clamped to `[200, 50000]`), and attachment metadata (`name`, `type`, `bytes`) for one message ID. Out-of-range or non-numeric mbox indices raise `ToolError`.

```jsonc
{
  "id": "inbox/2026.mbox#41",
  "date": "2026-11-20T09:12:00+09:00",
  "from": "…", "to": "…", "cc": "…", "subject": "…",
  "body": "…",
  "body_truncated": false,
  "attachments": [{ "name": "invoice.pdf", "type": "application/pdf", "bytes": 48213 }]
}
```

---

## 8. Media Worker (`worker.media`)

Media inspects, trims, and converts audio/video files under `--root` via `ffprobe` and `ffmpeg` (resolved on `PATH` at import time; missing binaries log a startup hint and raise `ToolError` on invocation).

**Paths & Time values:**
- Source and output paths must resolve to files strictly inside `--root` (the root directory itself and outside paths raise `ToolError`).
- Time arguments accept seconds (`"12.5"`) or `"HH:MM:SS(.ms)"`.

**Non-destructive output & failure cleanup:**
- Existing files are never overwritten: explicit `output` paths that already exist raise `ToolError`, automatic outputs (`<stem>_<tag><ext>`) append `_1`, `_2`, … until free, and `ffmpeg` always runs with `-n`.
- **Atomic cleanup:** If `ffmpeg` exits non-zero, times out, or is cancelled during `trim` or `convert`, any partial output file created on disk is unlinked immediately so subsequent retries to the same output path are not blocked.
- Subprocesses execute without a shell and with `stdin=DEVNULL` (`probe` timeout: `30s`; `trim`/`convert` timeout: `--timeout`, default `900s`). Non-zero exits surface the last 5 lines of `stderr` in `ToolError`.

### 8.1 `probe(path)`
Returns container summary and per-stream codec metadata (`duration_sec` and video `fps` rounded to 3 decimals, `null` when unavailable or `0/0`; `size_bytes` falls back to `stat().st_size`).
```jsonc
{
  "path": "clips/a.mp4",
  "container": "mov,mp4,m4a,3gp,3g2,mj2",
  "duration_sec": 63.52,
  "size_bytes": 10485760,
  "bit_rate": 1320000,
  "streams": [
    { "index": 0, "type": "video", "codec": "h264", "width": 1920, "height": 1080, "fps": 29.97 },
    { "index": 1, "type": "audio", "codec": "aac", "sample_rate": 48000, "channels": 2 }
  ]
}
```

### 8.2 `trim(path, start="0", end="", duration="", reencode=False, output="")`
Cuts a media segment into `<stem>_trim<ext>` (or `output`).
- Accepts either `end` or `duration`, not both; resulting segment length must be `> 0` (if neither is set, trims to EOF with `duration_sec: null`).
- Uses fast keyframe-snapped stream copy (`-c copy`) when `reencode=False`, or full re-encoding when `reencode=True`.
- Returns `{output, start_sec, duration_sec, reencoded}`.

### 8.3 `convert(path, format, output="")`
Transcodes or remuxes into `<stem>_conv.<format>` (or `output`). Leading `.` and case in `format` are normalized.

| Target kind | Allowed formats |
| :--- | :--- |
| **Audio** | `mp3`, `wav`, `m4a`, `aac`, `flac`, `ogg` (drops video track via `-vn`) |
| **Video** | `mp4`, `mkv`, `webm`, `mov`, `gif` |

Returns `{output, format, size_bytes}`.

---

## 9. GitHistory Worker (`worker.git.history`)

GitHistory executes read-only `git` queries against the repository containing `--root`. Missing `git` on `PATH` logs a startup hint and raises `ToolError` when called.

**Isolation & Lock-free Execution:**
- Subprocesses run without a shell, with `stdin=DEVNULL`, `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, `core.quotepath=off`, and `GIT_OPTIONAL_LOCKS=0`—preventing `.git/index.lock` contention with concurrent IDE or terminal Git operations. Timeout is `30s` per command.
- Dual boundary check: every `path` must resolve inside **both** `--root` and the repository toplevel (`git rev-parse --show-toplevel`). If `--root` points to a subdirectory of a larger monorepo, `search_commits` automatically scopes history to `--root`.
- Option-injection defense: `commit` revisions (`_REV_RE`) and `since` strings (`_DATE_RE`) enforce strict character allowlists rejecting leading `-` flags, paired with `--end-of-options` and `--` path separators.

### 9.1 `search_commits(path="", query="", author="", since="", limit=20)`
Lists commits newest first.
- `query` runs pickaxe search (`git log -S<query>`), matching commits that added or removed occurrences of the exact string.
- `author` filters `--author`; `since` sets `--since` (e.g., `'2026-01-01'`, `'2 weeks ago'`).
- `limit` is clamped to `[1, 200]`. Zero matches raise `ToolError("No matching commits.")`.

```jsonc
{
  "count": 1,
  "commits": [
    {
      "commit": "a1b2c3d4…",
      "author": "Fiber Bot",
      "email": "bot@fiber.local",
      "date": "2026-11-27T07:00:00+09:00",
      "subject": "refactor: update fee rate"
    }
  ]
}
```

### 9.2 `blame(path, start_line=1, end_line=0)`
Shows per-line last-modifying commit metadata for `path` (1-based line numbers; `path` must be an existing file).
- `start_line` must be `>= 1`. `end_line=0` defaults to `start_line + 99`.
- `end_line < start_line` raises `ToolError`; total span is capped at `400` lines (`start_line + 399`).

```jsonc
{
  "path": "src/calc.py",
  "lines": [
    {
      "line": 1,
      "commit": "a1b2c3d4e5f6",
      "author": "Fiber Bot",
      "date": "2026-11-27T00:00:00+00:00",
      "summary": "refactor: update fee rate",
      "text": "PICKAXE_TOKEN = 'FIBER_V2_RATE'"
    }
  ]
}
```

### 9.3 `show_commit(commit, path="", max_chars=8000)`
Returns commit header metadata, numstat file changes, and unified diff patch (optionally restricted to `path`).
- `max_chars` is clamped to `[500, 100000]`. Binary files report `added: null` and `deleted: null`.

```jsonc
{
  "commit": "a1b2c3d4…",
  "author": "Fiber Bot",
  "date": "2026-11-27T07:00:00+09:00",
  "message": "refactor: update fee rate",
  "files": [{ "path": "src/calc.py", "added": 2, "deleted": 1 }],
  "patch": "diff --git a/src/calc.py b/src/calc.py\n…",
  "patch_truncated": false
}
```