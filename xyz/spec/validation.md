# fiber.xyz.spec.validation
@desc: Pipeline benchmarks and end-to-end validation logs.

---

## 1. Pipeline Performance Benchmarks (Apple M2, 32GB RAM)

These metrics measure the **pure internal overhead** of the Fiber LLM Pipeline. To eliminate network variance and isolate framework performance, the suite was executed in deterministic **VCR Replay** mode.

**Run the benchmark with VCR driving 200 concurrent streams locally:**

```bash
fiber e2e inter.concurrency --vcr replay --concurrency 200
```

| Performance Dimension | Metric (M2, 32GB) | Architectural Significance |
| --- | --- | --- |
| **Unary Throughput** | **~1,180 req/s** | Sustains over 1,000 control-plane transactions per second in a single Python process. |
| **Stream Interleaving** | **~2,200 chunks/s** | High-density multiplexing of asynchronous SSE byte-streams without blocking the event loop. |
| **Routing Overhead** | **p50: <1.0ms / p95: <1.5ms** | Sub-millisecond framework latency added to Time-To-First-Token under concurrent bursts. |
| **Memory Footprint** | **<1 MB (Unary) / ~25 MB (Streams)** | Minimal GC pressure; buffers 200 active parallel streams within strict memory bounds. |
| **Context Isolation** | **Zero Cross-Talk** | Strict coroutine context isolation across parallel streams without state bleeding. |

---

## 2. System Validation Logs

Fiber enforces execution determinism and runtime safety through automated CI gates (on every PR and release tag) alongside targeted end-to-end validation suites across each subsystem:

### 2.1. Pre-Flight HITL over Slack

```bash
fiber e2e gateway.hitl.slack
```

Log: [hitl.slack.log](../log/gateway/hitl/slack.20261107.log)

Verifies that a third-party MCP call matching a pre-flight pattern is held before any I/O, resumes only after human approval over Slack (TOTP or 1-Click), and that an ephemeral stdio server is not spawned until approval.

| Target | Transport / Mode | Pre-flight Pattern | Approval |
| :--- | :--- | :--- | :--- |
| `net-context7` | NETWORK / MULTIPLEX | `/RESTRICTED_TOPIC\|DROP/i` | TOTP |
| `hitl-stdio-fs` | STDIO / EPHEMERAL | `/prod_secrets\|DELETE\|DROP/i` | 1-Click Button |

| Phase | Verifies | Status | Log Output |
| :--- | :--- | :--- | :--- |
| 1 | Connect Spec Resolver & Dual Mode (TOTP / Button) Guard Arming | PASSED | `Guard Armed: [net-context7-e2e-720141 -> NETWORK/MULTIPLEX (Mode: TOTP)] & [hitl-stdio-fs-e2e-720141 -> STDIO/EPHEMERAL (Mode: 1-CLICK BUTTON)]` |
| 2 | Safe Call Passthrough (Zero-Overhead on Non-Matching Payload) | PASSED | `Safe Stdio Call ('public_readme.txt') bypassed Pre-Flight Guard with 200 OK.` |
| 3 | [Network/Multiplex + TOTP] Context7 Trap & TOTP Attested Resume | PASSED | `Trapped 'RESTRICTED_TOPIC' prior to HTTP I/O`<br>`6-Digit TOTP Verified -> Context7 Stream Resumed (253 bytes)` |
| 4 | [Stdio/Ephemeral + 1-Click Button] Zero-Spawn Trap & Button-Only Resume | PASSED | `Zero-Spawn Confirmed (active_sandboxes=0 while parked)`<br>`Button-Only Approved -> Lazy npx Spawn -> File Read -> Sandbox Destroyed!` |

### 2.2. LLM Guardrail

```bash
fiber e2e gateway.llm.guardrail
```

Log: [llm.guardrail.log](../log/gateway/llm/guardrail.20261104.log)

Verifies that interceptors act in their pipeline slots: cache short-circuit, in-place masking before translation, blocking before egress, registry rule injection, and chained defense with fallback.

| Phase | Verifies | Status | Total | Tokens | Log Output |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | Baseline Tracer & Edge Cache Short-Circuit | PASS | 911.12ms | 19 | `[CACHE HIT] Match found! Short-circuiting physical I/O...` |
| 2 | PRE_TRANSLATE In-Place PII Masking | PASS | 48.93ms | 27 | `Passed: PRE_TRANSLATE In-Place PII Masking` |
| 3 | POST_TRANSLATE Strict Blocking (Built-in & Custom) | PASS | 2.71ms | Blocked | `Passed: POST_TRANSLATE Strict Blocking (Built-in & Custom)` |
| 4 | Secret Registry Dynamic Rule Injection (DIP) | PASS | 14.43ms | Blocked | `[REDACT - broken_regex_rule] Invalid regex skipped ('[INVALID_UNCLOSED_REGEX')` |
| 5 | Chained Defense (Mask->Block) & Fallback Shield | PASS | 136.86ms | 18 | `Fallback attempt triggered. Retrying with model: llama_server/gemma-3-1b-it-Q4_K_M.gguf` |

### 2.3. LLM VCR

```bash
fiber e2e gateway.llm.vcr --vcr replay --model cerebras/qwen-3.8-27b
```

Log: [llm.vcr.log](../log/gateway/llm/vcr.20261104.log)

Verifies that the pipeline and tracer behave the same under VCR replay: sync and stream calls replay from fixtures with no upstream I/O, exceptions surface at the tracer boundary, and fallback retries replay from fixtures.

| Phase | Verifies | Status | Total | I/O | Overhead | Log Output |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | Singular Tracing Isolation | PASS | 16.32ms | 1334.49ms | 0.00ms | `Replayed sync fixture '9b38cf61' in 0.26ms (original live: 1334.5ms).` |
| 2 | Asynchronous Stream Tracking | PASS | 114.45ms | 1229.07ms | 0.00ms | `Replayed stream fixture '7c7f8484' (7 chunks in 101.07ms, 0ms upstream I/O).` |
| 3 | Exception Boundary Verification | PASS | 11.93ms | — | 11.93ms (Overhead Only) | `[LLM CALL FAILED] Error: GateBadRequestError - LLM Provider NOT provided. … You passed model=invalid/fake` |
| 4 | Fallback & Deepcopy Shielding | PASS | 12.98ms | 365.00ms | 0.00ms | `Fallback attempt triggered. Retrying with model: cerebras/qwen-3.8-27b`<br>`Replayed sync fixture '4b0491ab' in 0.20ms (original live: 365.0ms).` |

### 2.4. Host WASM Plane (`phase.wasm`)

```bash
fiber e2e infra.wasm.phase
```

Verifies the `phase.wasm` kernel on the host-native plane (`XPHI_WASM_PLANE=local`): deterministic execution (frozen clock, injected seed, fixed PRNG and floating-point results), sandbox isolation under SYSTEM / STANDARD resource tiers, parity recovery, multi-sig epoch sealing across multi-agent lifecycles, and field-dynamics kernels. The full run is sealed into a canonical execution proof. PRNG output and the Byzantine 2-of-3 commit hash match the Edge V8 Plane run (2.5).

| Suite | Verifies | Scenarios | Status | Log Output |
| :--- | :--- | :--- | :--- | :--- |
| SANDBOX | WasmCG guard, sandbox isolation (file, socket, env, subprocess, thread, `sys.exit`), timeout & memory trap, clock/seed determinism, ID generation, tripartite parity, FFI robustness, fuel profiling | 24 | PASS | `Isolated: True`<br>`PRNG sequences are 100% identical (0.7946015362095357\|71a45460)`<br>`wasm trap: all fuel consu…` |
| ANCHOR | Provenance fingerprinting, epoch init & XOR parity recovery, epoch sealing, 3-of-3 settlement, AnchorStore multi-sig boundaries (2-of-3, insufficient, Sybil, ACL), 5-flow epoch lifecycle (AI Agent Swarm Consensus, Cross-Repo Provenance Alignment) | — | PASS | `UNAUTHORIZED_PROPOSER (Consensus Failed): Duplicate signer detected: …`<br>`Epoch Sealed Successfully via Multi-sig Consensus.` |
| CERT | Fuel quota trap, floating-point determinism, Byzantine quarantine (2-of-3) | 3 | PASS | `Quota Error: CPU Fuel Quota Exceeded (10000000)`<br>`Perfect Determinism achieved. Result: 1.595196469450608`<br>`commit_hash: f099fdc0738917003d8e6ed0cfaad3bb9e784cd82503b304494fa90204c36c8f` |
| DYNAMICS | Kuramoto oscillator sync, FitzHugh-Nagumo action potential, attractor/reflector bypass, dynamics FFI guard | 5 | PASS | `Kuramoto Math Verified. d_phase: 0.1000`<br>`FHN Spiking logic and high tension (100.0) accurately triggered.` |

Canonical proof: `Test Execution Proof successfully sealed! Hash: 8ba8249ea78558d073908077f545f46cd221719522d5f22ac44024e599e598c8`

### 2.5. Edge V8 Plane

```bash
fiber e2e infra.plane.flare
```

Log: [infra.plane.flare.log](../log/infra/plane/flare.20261106.log)

Verifies that `XPHI_WASM_PLANE` binds the execution broker to local Dual V8 Isolates (Wasm Gateway `:8787`, Pyodide Engine `:8788`), that sandboxed code cannot reach host resources, that computation is deterministic across runs, and that edge results are sealed into the local PTA ledger.

| Suite | Group | Verifies | Scenarios | Status | Log Output |
| :--- | :--- | :--- | :--- | :--- | :--- |
| BRIDGE | Bridge 1 | XPHI_WASM_PLANE Config Resolution & Singleton Isolation | 1 | PASS | `WasmBroker.get_shared() auto-bound to FlareTunnelFactory via XPHI_WASM_PLANE` |
| BRIDGE | Bridge 2 | Control Channel, PHASE Wasm ABI & PYTHON Service Binding | 4 | PASS | `PHASE VM (phase.wasm) direct ABI call verified`<br>`PYTHON VM Service Binding & X-XPHI-Canonical-Hash seal verified (output=42, hash=5a39840d4f18...)` |
| BRIDGE | Bridge 3 | Local-Edge Transaction Hash Rollup | 1 | PASS | `Bridge transactions sealed into Local PTA Merkle Root: 76f2b80d4e6cfe6b...` |
| FLARE | Domain 1 | V8 Isolate & Wasm Runtime Boundary Isolation | 8 | PASS | `FileNotFoundError: [Errno 44] No such file or directory: '/etc/passwd'`<br>`Metadata SSRF attempt intercepted by Isolate network boundary.` |
| FLARE | Domain 2 | Causality (Epoch-Tick) & Parity Validation | 2 | PASS | `At least two identification IDs are required for parity recovery and correction` |
| FLARE | Domain 3 | Computation Determinism & Byzantine Faults | 3 | PASS | `PRNG sequences are deterministic across runs`<br>`IEEE-754 FP Determinism verified across 3 runs: 13.093669021622972` |
| FLARE | Domain 4 | Cloudflare Worker Side-Channel Guards | 2 | PASS | `Isolate context verified clean (Zero warm-start state leakage).`<br>`Deterministic clock freezing verified (Timer side-channel mitigated).` |
| FLARE | Domain 5 | PTA Ledger & Epoch Merkle Sealing | 1 | PASS | `PTA Rollup Complete. Epoch Merkle Root: 3f67833f405c2e0f...` |
| FLARE | Domain 6 | Edge Isolate Runaway Execution Cutoff | 1 | PASS | `Broker timed out. Runaway execution neutralized via Wall-Time cutoff.` (3000.40ms) |

Domain 1 scenarios: unregistered API block, host filesystem scan, low-level socket binding, host environment variable leakage, subprocess restriction, single-thread enforcement, `sys.exit()` engine shutdown, metadata SSRF.

### 2.6. FinOps Budget & Kill-Switch

```bash
fiber e2e inter.finops --vcr replay
```

Verifies the tenant budget lifecycle and kill-switch path: a FinOps JWT is translated to an AP2 Mandate and synced to the budget watcher, duplicate JTIs are ignored, a higher-version top-up is applied, a stale version is rejected, and a Pub/Sub REVOKE severs an active stream. The LLM stream is replayed from a VCR fixture, so the cutoff is reproduced without a provider call.

| Phase | Verifies | Status | Log Output |
| :--- | :--- | :--- | :--- |
| 1 | PTA Infrastructure Setup | PASSED | `Produced Balance for 'accounting_dept_e2e_93d96046': 1000 Fuel` |
| 2 | Track B - FinOps JWT Issuance, Idempotency & Lifecycle Translation | PASSED | `TOP_UP executed: Tenant 'accounting_dept_e2e_93d96046' +10,000,000 -> 60,000,000 Fuel (v2).`<br>`Rejected outdated token version (v1) for tenant 'accounting_dept_e2e_93d96046'.` |
| 3 | Track A - Dynamic Pub/Sub Kill Switch vs VCR Stream | PASSED | `Published KILL-SWITCH (REVOKE) for tenant 'accounting_dept_e2e_93d96046' via Pub/Sub`<br>`FuelTrap dynamically intercepted and killed the long VCR stream at chunk 1 (2 chars).` |

Phase 2 checks: JWT issued by FinOps worker (SYNC_QUOTA, v1) → translated to AP2 Mandate, budget synced to 50,000,000 Fuel → duplicate JTI exchange ignored → v2 TOP_UP applied (60,000,000 Fuel) → v1 token replay rejected.

### 2.7. Built-in Workers

```bash
fiber e2e worker.builtin
```

Log: [worker.builtin.log](../log/worker/builtin.20261127.log)

Verifies all seven built-in workers through the full HTTP Gateway (DPoP + Nonce) → Pub/Sub Tunnel → Connector → Worker path. Each target mounts under a run-scoped ID (`e2e.<target>.<hex>`) so the suite runs cleanly alongside a live `fiber daemon` without queue collisions. Seed data from `reflexor/fixtures/mcp/builtin` is materialized into a temporary workspace (`--root`), and Pin and Reader bind `--db` to temporary SQLite files so `~/.anchor` is never touched. Evaluates snapshot contracts via `diff_snapshot` and verifies that invalid inputs across all workers return recoverable tool errors (`TOOL_ERROR_200`, `isError: true`) rather than `HTTP 502` gateway faults.

| Target | Module | Transport / Mode |
| :--- | :--- | :--- |
| `duck` | `fiber.node.worker.duck` | STDIO / MULTIPLEX |
| `verifier` | `fiber.node.worker.dev.verifier` | STDIO / MULTIPLEX |
| `mailbox` | `fiber.node.worker.daily.mailbox` | STDIO / MULTIPLEX |
| `media` | `fiber.node.worker.daily.media` | STDIO / MULTIPLEX |
| `reader` | `fiber.node.worker.reader` | STDIO / MULTIPLEX |
| `pin` | `fiber.node.worker.memory.pin` | STDIO / MULTIPLEX |
| `git` | `fiber.node.worker.dev.git.history` | STDIO / MULTIPLEX |

| Phase | Verifies | Status | Log Output |
| :--- | :--- | :--- | :--- |
| 1 | 7-Worker Boot, Schema Snapshot & Unified Route | PASSED | `[discovery.unified_describe_csv] -> OK` |
| 2 | [Duck] Fixture Cases & 10x Async Concurrency | PASSED | `[duck.describe_file_clause_a2] -> OK`<br>`[duck.block_copy_egress] -> TOOL_ERROR_200`<br>`[duck.block_outside_path] -> TOOL_ERROR_200` |
| 3 | [Verifier] Fixture Cases | PASSED | `[verifier.list_rules_schema] -> OK`<br>`[verifier.reject_malformed_json] -> TOOL_ERROR_200` |
| 4 | [Mailbox] Fixture Cases & Symlink Escape Audit | PASSED | `[mailbox.read_mbox_by_index] -> OK`<br>`[mailbox.block_symlink_escape] -> TOOL_ERROR_200` |
| 5 | [Media] Fixture Cases (FFprobe/FFmpeg) | PASSED | `[media.convert_wav_to_flac] -> OK`<br>`[media.cleanup_partial_on_ffmpeg_fail_md2] -> TOOL_ERROR_200` |
| 6 | [Reader] Partial-Sync Guard, Scoped Retrieval & Table Diff | PASSED | `[reader.step2_full_outline_after_partial] -> OK`<br>`[reader.compare_markdown_table_to_csv] -> OK` |
| 7 | [Pin] Full Lifecycle Fixture Cases | PASSED | `[pin.step5_scope_with_locked_base_rev] -> OK`<br>`[pin.step6_drop_pin] -> OK` |
| 8 | [GitHistory] Commit/Blame/Show Fixture Cases | PASSED | `[git.pickaxe_commit_search] -> OK`<br>`[git.show_head_patch] -> OK` |
| 9 | [Cross-Worker] 12-Case Error Contract Audit | PASSED | `[error_contracts.reader_escape] -> TOOL_ERROR_200`<br>`[error_contracts.verifier_empty_probe] -> TOOL_ERROR_200` |

Phase 2 cases: ellipsis literal (`'wait...now'`) in a string filter, standalone `DESCRIBE '<file>'` mounting, blocking of `COPY` egress, `ATTACH`, and outside-root paths, plus 10 concurrent multiplexed queries.

Phase 4 cases: `.eml` search with attachment metadata, `.mbox` read by `#<index>`, skipping of a symlink pointing outside `--root` (yielding no match), and rejection of offset timestamps in `since` and `.mbox` IDs missing `#<index>`.

Phase 5 cases: `ffprobe` stream inspection, segment trim, WAV → FLAC conversion, duplicate-output overwrite guard, and atomic partial-file cleanup after a failed `ffmpeg` encode (confirmed absent on subsequent probe).

Phase 6 cases: subfolder-first partial sync followed by full-root outline expansion, scoped token retrieval, and Markdown section table vs. CSV join diffing.

Phase 9 cases (12): `duck_bad_sql`, `mailbox_bad_date`, `mailbox_bad_id`, `media_bad_time`, `media_bad_fmt`, `reader_escape`, `reader_empty_q`, `pin_empty_key`, `pin_empty_addrs`, `git_bad_rev`, `git_bad_range`, `verifier_empty_probe`.