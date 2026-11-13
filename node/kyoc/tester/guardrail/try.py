# fiber.node.kyoc.tester.guardrail.try
# /// script
# requires-python = ">=3.12"
# dependencies = ["fiber @ git+https://github.com/dphilabs/fiber.git"]
# ///
"""
Fiber — Zero-Key In-Process LLM Guardrail (PII Masking & Secret Blocking) + Deterministic VCR Stream Replay.
Docs: https://fiber.dphilabs.com | Repo: https://github.com/dphilabs/fiber

Run via CLI:   fiber kyoc tester.guardrail.try
Run via URL:   uv run https://raw.githubusercontent.com/dphilabs/fiber/main/node/kyoc/tester/guardrail/try.py
"""
import asyncio
from fiber.llm.entry import acompletion
from fiber.llm.pipeline.interceptor.guardrail import (
    PayloadSanitizationGuardrail,
    CustomRegexMaskingGuardrail,
    ActiveBlockingGuardrail,
)
from fiber.llm.vcr.gist.setup import vcr_gist_session


async def main():
    # Emulates recorded stream timing (delta_ms) from offline VCR fixtures with zero API keys
    with vcr_gist_session(speed="real"):
        # PRE_TRANSLATE: Masks soft PII in-place before hitting the model or VCR adapter
        stream = await acompletion(
            model="anthropic/claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "My phone is 010-1234-5678. Say 'Received'."}],
            stream=True,
            interceptors=[
                PayloadSanitizationGuardrail(),
                CustomRegexMaskingGuardrail([r"\b010-\d{4}-\d{4}\b"]),
            ],
        )

        print("[VCR Stream] ", end="", flush=True)
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            print(delta, end="", flush=True)
            await asyncio.sleep(0.08)
        print("\n")

        # POST_TRANSLATE: Hard-blocks leaked credentials (33 built-in rules) prior to any I/O
        try:
            await acompletion(
                model="gemini/gemini-3.1-flash-lite",
                messages=[
                    {
                        "role": "user",
                        "content": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
                    }
                ],
                interceptors=[ActiveBlockingGuardrail()],
            )
        except Exception as e:
            print(f"[DLP Guard ] Blocked in-flight -> {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())