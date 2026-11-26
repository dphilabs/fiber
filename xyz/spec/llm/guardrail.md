# fiber.xyz.spec.llm.guardrail
@desc: Built-in guardrails, secret registry, and kill-switch binding.

Gateway calls (`fiber daemon`) run with default options. The code in this spec is for customization through the Python SDK. For building new interceptors, see the **[Interceptor Spec](./interceptor.md)**.

---

## Tracks

| Track | Slot | On Match | Classes |
| :--- | :--- | :--- | :--- |
| **Sanitize** | `PRE_TRANSLATE` | Mask in-place, continue | `PayloadSanitizationGuardrail`, `CustomRegexMaskingGuardrail` |
| **Block** | `POST_TRANSLATE` | Abort with `PermissionError` | `ActiveBlockingGuardrail`, `CustomRegexBlockingGuardrail` |

Slot execution order: [Interceptor Spec §1](./interceptor.md#1-pipeline-topology--slot-execution-order).

---

## Built-in Guardrails (`fiber.llm.pipeline.interceptor.guardrail`)

```python
from typing import Any, List
from fiber.llm.pipeline.bootstrap import PipelineSlot
from fiber.llm.compat.secret.redact import redact_string, sanitize_payload, FastRegexRedactionEngine
from xphi.state.phase.channel import DuplexChannel, ChannelContext

# [Sanitize] Recursive dict / header / URL query sanitization
class PayloadSanitizationGuardrail(DuplexChannel):
    target_slot = PipelineSlot.PRE_TRANSLATE
    async def write(self, ctx: ChannelContext, msg: dict):
        await ctx.fire_write(sanitize_payload(msg))

# [Sanitize] In-place regex PII masking
class CustomRegexMaskingGuardrail(DuplexChannel):
    target_slot = PipelineSlot.PRE_TRANSLATE
    def __init__(self, custom_patterns: List[str]):
        super().__init__()
        self.engine = FastRegexRedactionEngine(patterns=custom_patterns)

    async def write(self, ctx: ChannelContext, msg: dict):
        for m in msg.get("messages", []):
            if isinstance(m, dict) and isinstance(m.get("content"), str):
                m["content"] = self.engine.execute(m["content"].encode("utf-8")).decode("utf-8", errors="replace")
        await ctx.fire_write(msg)

# [Block] Built-in + registry credential blocking
class ActiveBlockingGuardrail(DuplexChannel):
    target_slot = PipelineSlot.POST_TRANSLATE
    async def write(self, ctx: ChannelContext, processed_msg: Any):
        for m in getattr(processed_msg, "original_kwargs", {}).get("messages", []):
            content = m.get("content", "")
            if isinstance(content, str) and redact_string(content) != content:
                return await ctx.fire_exception_caught(
                    PermissionError("Guardrail Block: Sensitive credential detected.")
                )
        await ctx.fire_write(processed_msg)

# [Block] Custom regex policy blocking
class CustomRegexBlockingGuardrail(DuplexChannel):
    target_slot = PipelineSlot.POST_TRANSLATE
    def __init__(self, custom_patterns: List[str]):
        super().__init__()
        self.engine = FastRegexRedactionEngine(patterns=custom_patterns)

    async def write(self, ctx: ChannelContext, processed_msg: Any):
        for m in getattr(processed_msg, "original_kwargs", {}).get("messages", []):
            content = m.get("content", "")
            if isinstance(content, str) and self.engine.contains_match(content.encode("utf-8")):
                return await ctx.fire_exception_caught(
                    PermissionError("Guardrail Block: Custom domain policy violation.")
                )
        await ctx.fire_write(processed_msg)
```

---

## Secret Registry (`fiber.llm.compat.secret.registry`)

### Built-in Rules

33 credential regex rules (`BASE_SECRET_RULES`) and 14 sensitive key/URL tokens (`SECRET_KEY_PATTERNS`):

* **Cloud & Infra (`7`):** `aws_access_key_id`, `aws_kv_credential`, `gcp_api_key`, `gcp_oauth_token`, `gcp_service_account_json`, `azure_kv_secret`, `databricks_token`
* **Auth & Private Keys (`6`):** `bearer_token`, `basic_auth`, `uri_basic_auth`, `jwt_token`, `pem_private_key`, `inline_private_key_kv`
* **LLM & AI Vendors (`9`):** `llm_vendor_key` *(OpenAI/Anthropic/OpenRouter)*, `openai_legacy_key`, `openhands_key`, `groq_key`, `huggingface_token`, `together_ai_key`, `context7_key`, `claude_web_key`, `tavily_api_key`
* **SaaS & Generic KV (`11`):** `github_token`, `stripe_key`, `slack_webhook`, `slack_token`, `sentry_token`, `linear_api_key`, `jira_token`, `generic_api_key_kv`, `generic_password_kv`, `generic_config_secret_kv`, `url_query_secret`
* **Sensitive Keys (`14`):** `AUTHORIZATION`, `CERTIFICATE`, `COOKIE`, `CREDENTIAL`, `JWT`, `KEY`, `OAUTH`, `PASSPHRASE`, `PASSWORD`, `PRIVATE`, `SECRET`, `SESSION`, `SIGNATURE`, `TOKEN`

### `redact_rules.json`

Loaded from the registry directory, `FIBER_REDACT_RULES_PATH`, or `RedactRegistryIO.initialize_and_inject(custom_path)`. Each regex is validated at boot; malformed rules are skipped with a warning.

```json
{
  "patterns": {
    "fiber_internal_token": "FIBER-INTERNAL-TOKEN-[0-9]{6}",
    "corp_vault_key": "CORP-VAULT-[A-Z0-9]{16}",
    "slack_token": "xox[baprs]-[A-Za-z0-9_-]{24,}"
  },
  "disabled_rules": ["context7_key", "generic_password_kv"],
  "extra_secret_keys": ["FIBER_VAULT_PIN", "INTERNAL_AUTH_HEADER"]
}
```

| Field | Type | Description |
| :--- | :--- | :--- |
| `patterns` | `Dict[str, str]` | Adds a rule, or overrides a built-in rule with the same ID. |
| `disabled_rules` | `List[str]` | Disables built-in rules by exact rule ID. |
| `extra_secret_keys` | `List[str]` | Case-insensitive key tokens (min. 3 chars) for `sanitize_payload`. |

---

## Where Guardrails Apply

### Gateway

Requests to `fiber daemon` (`/v1/chat/completions`) run with `PRE_TRANSLATE` masking and `POST_TRANSLATE` blocking enabled. No code is needed. Toggle each slot in the **Guardrail** tab of `fiber console`; settings persist in `~/.anchor/state.json`.

To add guardrails for a single request, pass `interceptors` in the payload:

```bash
$ curl -X POST http://127.0.0.1:8356/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini/gemini-3.1-flash-lite",
    "messages": [{"role": "user", "content": "Contact 010-1234-5678 regarding CONFIDENTIAL-99."}],
    "interceptors": [
      {"class": "guardrail.CustomRegexMaskingGuardrail", "kwargs": {"custom_patterns": ["\\b010-\\d{4}-\\d{4}\\b"]}},
      {"class": "guardrail.CustomRegexBlockingGuardrail", "kwargs": {"custom_patterns": ["CONFIDENTIAL-[0-9]+"]}}
    ]
  }'
```

### SDK (custom)

In-process calls run only the interceptors passed in `interceptors=[...]`.

```python
from fiber.llm.entry import acompletion
from fiber.llm.pipeline.interceptor.guardrail import (
    PayloadSanitizationGuardrail,
    CustomRegexMaskingGuardrail,
    ActiveBlockingGuardrail,
)

response = await acompletion(
    model="lm_studio/qwen-3.5-9b",
    messages=[{"role": "user", "content": "Customer phone: 010-1234-5678. Summarize inquiry."}],
    stream=True,
    interceptors=[
        PayloadSanitizationGuardrail(),
        CustomRegexMaskingGuardrail([r"\b010-\d{4}-\d{4}\b"]),
        ActiveBlockingGuardrail(),
    ],
)
```

---

## Kill-Switch & Metering

`StreamBreaker` cuts a stream when the tenant's quota runs out or a kill signal arrives. `MeteringObserver` records token usage against the tenant's balance in the local ledger.

### Gateway

`fiber daemon` binds both and runs the budget watcher. Budgets and kill signals are issued with `fiber account` (see [README §3.1](../../../README.md#31-fiber-connect-cli)).

### SDK (custom)

```python
import os
from fiber.llm.entry import acompletion
from fiber.llm.pipeline.channel.breaker import StreamBreaker
from fiber.llm.pipeline.channel.observer import MeteringObserver
from fiber.node.account.finops.watcher import default_finops_watcher, default_tenant_eco
from fiber.node.account.auth.payload import NodeAuthPayload
from xphi.arch.bound.adapter.pta import PtaAdapter
from xphi.node.runtime.wasm.broker import WasmBroker

os.environ["FIBER_ENFORCE_QUOTA"] = "true"

broker = WasmBroker(timeout=5.0)
pta_adapter = PtaAdapter(broker=broker)

default_finops_watcher.start()                  # Receive budget & kill signals
await default_finops_watcher.wait_until_ready()
try:
    stream = await acompletion(
        model="cerebras/qwen-3.8-27b",
        messages=[{"role": "user", "content": "..."}],
        stream=True,
        stream_breaker=StreamBreaker(finops_watcher=default_finops_watcher),
        metering_observer=MeteringObserver(tenant_eco=default_tenant_eco, pta_adapter=pta_adapter),
        metadata={"node_auth": NodeAuthPayload(tenant_id="accounting_dept", fuel_budget=50000, is_enforced=True)},
    )
    async for chunk in stream:
        ...
finally:
    default_finops_watcher.stop()
    await broker.close()
```

`StreamBreaker` runs upstream of the VCR proxy: in `vcr=record` only pre-cutoff chunks are saved, and `vcr=replay` reproduces the same cutoff.

---

## Validation

- **[llm.guardrail.log](../../log/gateway/llm/guardrail.20261104.log)**
- Kill-switch & metering: `fiber e2e inter.finops`