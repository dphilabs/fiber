# fiber.studio.cli.ops.channel.extoob
"""
Lightweight Injector & Socket Mode Tester for ExtendedOobBridge (`extoob`).
Usage Examples:
  # Zero-Token Offline Simulation (Tests UI block generation + override/abort extraction)
  $ fiber ops channel.extoob sim

  # Live Slack Socket Mode Test (Domain-agnostic technical defaults)
  $ fiber ops channel.extoob live --bot-token "xoxb-..." --app-token "xapp-..."

  # Live Test with External Preset Injection (Custom labels, steering, abort tags)
  $ fiber ops channel.extoob live --preset-json ./custom_preset.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

from fiber.gateway.bridge.channel.extoob import ExtendedOobBridge
from fiber.gateway.bridge.channel.schema import InteractiveUiLabels, SelectOption
from fiber.studio.cli.ops.channel.slack import (
    LocalTOTPVerifiedMockRpc,
    discover_slack_channel,
    get_local_spiffe_id,
    validate_slack_tokens,
)


def build_bridge_from_preset(
    preset_path: Optional[str],
    rpc: Any = None,
    bot_token: str = "",
    app_token: str = "",
    channel_id: str = "",
) -> tuple[ExtendedOobBridge, Dict[str, Any]]:
    """
    외부 JSON 파일(preset_path)이 주어지면 UI 라벨과 옵션을 브릿지에 주입하고,
    없으면 순수 기술 용어 기본값(Technical Defaults)으로 초기화합니다.
    """
    cfg: Dict[str, Any] = {}
    if preset_path:
        with open(preset_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    labels = InteractiveUiLabels(**cfg.get("ui_labels", {}))
    abort_reasons = (
        tuple(SelectOption.from_raw(x) for x in cfg["abort_reasons"])
        if "abort_reasons" in cfg
        else ExtendedOobBridge().default_abort_reasons
    )

    bridge = ExtendedOobBridge(
        rpc=rpc,
        bot_token=bot_token,
        app_token=app_token,
        default_channel=channel_id,
        default_labels=labels,
        default_abort_reasons=abort_reasons,
    )

    # 테스트용 기본 프롬프트 파라미터 (외부 JSON의 prompt_params로 덮어쓰기 가능)
    default_prompt_params: Dict[str, Any] = {
        "interaction_profile": "standard_ext",
        "summary_text": "Target: `worker.dispatch` | Policy: `PREFLIGHT_INTERCEPT`",
        "steering_options": [
            {"value": "MODE_STANDARD", "label": "Standard Execution"},
            {"value": "MODE_ELEVATED", "label": "Elevated Audit Execution"},
        ],
        "editable_args": {
            "payload_body": "SELECT id, status FROM ledger_entries LIMIT 10;"
        },
        "lease_options": True,
        "allow_comment": True,
    }
    if "prompt_params" in cfg:
        default_prompt_params.update(cfg["prompt_params"])

    return bridge, default_prompt_params


async def run_offline_simulation(preset_path: Optional[str] = None) -> None:
    """Slack 연동 없이 로컬에서 즉시 Block 생성 -> 인자 수정/반려 파싱 -> OOB 디스패치를 검증합니다."""
    rpc = LocalTOTPVerifiedMockRpc(require_totp=False)
    bridge, prompt_params = build_bridge_from_preset(preset_path, rpc=rpc)
    spiffe_id = get_local_spiffe_id()
    bridge.register_identity_mapping("U_SIM_OPERATOR", spiffe_id)

    handle_id = f"sim_{int(time.time()) % 10000}"
    yield_event = {
        "handle_id": handle_id,
        "target_server_id": "mcp.sandbox.worker",
        "agent": spiffe_id,
        "prompt": {"id": f"prompt_{handle_id}", "params": prompt_params},
    }

    msg = bridge.build_yield_message_blocks(yield_event, channel="C_SIM")
    token_str = msg["blocks"][-1]["elements"][0]["value"]
    print(f"[Sim] Generated {len(msg['blocks'])} Block Kit blocks (Handle: {handle_id}).")
    mock_slack_action = {
        "type": "block_actions",
        "user": {"id": "U_SIM_OPERATOR"},
        "actions": [{"action_id": "fiber_action_approve", "value": token_str}],
        "state": {
            "values": {
                bridge.BLK_STEERING: {
                    bridge.ACT_STEERING: {"selected_option": {"value": "MODE_ELEVATED"}}
                },
                bridge.BLK_OVERRIDE: {
                    bridge.ACT_OVERRIDE: {"value": "SELECT id FROM ledger_entries LIMIT 1;"}
                },
                bridge.BLK_COMMENT: {
                    bridge.ACT_COMMENT: {"value": "Verified query scope down to LIMIT 1"}
                },
                bridge.BLK_LEASE: {
                    bridge.ACT_LEASE: {"selected_option": {"value": "900"}}
                },
            }
        },
    }

    normalized = bridge.extract_interaction_from_block_actions(mock_slack_action)
    ts = str(int(time.time()))
    raw_bytes = json.dumps(normalized, separators=(",", ":")).encode("utf-8")
    import hashlib, hmac
    sig = "v0=" + hmac.new(bridge.slack_secret, f"v0:{ts}:".encode() + raw_bytes, hashlib.sha256).hexdigest()

    await bridge.handle_slack_interaction(ts, raw_bytes, sig, normalized)
    print("[✓] Offline Simulation Succeeded!")


async def run_live_extoob_test(
    bot_token: str,
    app_token: str,
    channel_id: str = "",
    preset_path: Optional[str] = None,
    require_totp: bool = False,
    timeout_sec: int = 120,
) -> bool:
    """실제 Slack 채널에 확장 Interactive 카드를 띄우고 클릭 결과를 수신합니다."""
    b_tok, a_tok = validate_slack_tokens(bot_token, app_token)
    target_channel = channel_id.strip() or await discover_slack_channel(b_tok)
    if not target_channel:
        return False

    spiffe_id = get_local_spiffe_id()
    rpc = LocalTOTPVerifiedMockRpc(require_totp=require_totp)
    bridge, prompt_params = build_bridge_from_preset(
        preset_path, rpc=rpc, bot_token=b_tok, app_token=a_tok, channel_id=target_channel
    )
    prompt_params["require_totp"] = require_totp
    bridge.register_identity_mapping("*", spiffe_id)

    handle_id = f"ext_{int(time.time()) % 10000}"
    yield_event = {
        "event": "MCP_PHASE_YIELD",
        "handle_id": handle_id,
        "target_server_id": "mcp.sandbox.worker",
        "agent": spiffe_id,
        "prompt": {"id": f"prompt_{handle_id}", "params": prompt_params},
    }

    bridge.running = True
    daemon_task = asyncio.create_task(bridge.run_socket_mode_daemon())
    await asyncio.sleep(1.2)

    print(f"\n[ExtOobTester] Posting Interactive Card -> {target_channel} (Handle: {handle_id})")
    ts = await bridge.post_yield_notification(yield_event)
    if not ts:
        bridge.running = False
        daemon_task.cancel()
        return False

    print(f"[ExtOobTester] Modify inputs in Slack and click Approve/Deny (Waiting {timeout_sec}s)...")
    try:
        await asyncio.wait_for(rpc.resolved_event.wait(), timeout=timeout_sec)
        await asyncio.sleep(1.0)
        print("[✓] Live Extended OOB round-trip succeeded!")
        return True
    except asyncio.TimeoutError:
        print("[!] Timed out waiting for Slack interaction.")
        return False
    finally:
        bridge.running = False
        daemon_task.cancel()
        await asyncio.gather(daemon_task, return_exceptions=True)


async def _async_main() -> None:
    parser = argparse.ArgumentParser(description="ExtendedOobBridge Injector & Tester")
    parser.add_argument("mode", nargs="?", default="sim", choices=["sim", "live"])
    parser.add_argument("--bot-token", default=os.environ.get("FIBER_TEST_SLACK_BOT_TOKEN", ""))
    parser.add_argument("--app-token", default=os.environ.get("FIBER_TEST_SLACK_APP_TOKEN", ""))
    parser.add_argument("--channel", default="")
    parser.add_argument("--preset-json", default=None, help="Path to external JSON preset for domain injection")
    parser.add_argument("--totp", action="store_true")
    args = parser.parse_args()

    if args.mode == "sim":
        await run_offline_simulation(preset_path=args.preset_json)
    else:
        b_tok = args.bot_token or input("1) Slack Bot Token (xoxb-...): ").strip()
        a_tok = args.app_token or input("2) Slack App Token (xapp-...): ").strip()
        await run_live_extoob_test(
            bot_token=b_tok,
            app_token=a_tok,
            channel_id=args.channel,
            preset_path=args.preset_json,
            require_totp=args.totp,
        )


if __name__ == "__main__":
    asyncio.run(_async_main())