# fiber.dev.e2e.gateway.hitl.slack
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fiber.dev.base.mcp.payload import McpPayloadBuilder
from fiber.dev.base.workflow.audit.context import AuditContext, MCPGatewayTestClient
from fiber.dev.base.workflow.config import E2EConfig, Phase, TestResult, get_free_port
from fiber.dev.base.workflow.mcp import MockMcpServer
from fiber.dev.base.workflow.pipeline import MockPipeline, log
from fiber.gateway.bridge.channel.slack import SlackHitlBridge

from xphi.arch.contract.config.env import DEFAULT_GATEWAY_PORT
from xphi.state.phase.reactor import PhaseReactor


class ThirdPartyPreflightHitlPipeline(MockPipeline):
    def __init__(self, config: E2EConfig):
        super().__init__(
            config=config,
            name="3rd-Party Zero-Code Pre-Flight HITL (TOTP & 1-Click Button) Suite",
            scope_name="MCP_PREFLIGHT_HITL_SUITE",
        )
        self.mcp = McpPayloadBuilder()
        self.crypto_ctx = AuditContext(spiffe_id="spiffe://self/fiber")
        self.gw_client: Optional[MCPGatewayTestClient] = None

        self.net_target_id = self.crypto_ctx.scoped_target("net-context7")
        self.stdio_target_id = self.crypto_ctx.scoped_target("hitl-stdio-fs")

        self.env_key_name = "CONTEXT7_API_KEY"
        user_key = os.environ.get(self.env_key_name, "").strip()
        self._injected_temp_env = not bool(user_key)
        self.active_api_key = user_key or f"ctx7_live_e2e_{uuid.uuid4().hex[:16]}"
        os.environ[self.env_key_name] = self.active_api_key

        self.mock_mcp = MockMcpServer(
            host="127.0.0.1", port=get_free_port(8360), expected_api_key=self.active_api_key
        )
        self.context7_url = os.environ.get("CONTEXT7_MCP_URL", "").strip() or self.mock_mcp.context7_url

        self.temp_dir = tempfile.TemporaryDirectory(prefix="fiber_hitl_fs_")
        self.sandbox_path = Path(self.temp_dir.name).resolve()
        (self.sandbox_path / "public_readme.txt").write_text("SAFE_PUBLIC_CONTENT", encoding="utf-8")
        (self.sandbox_path / "prod_secrets.env").write_text("SUPER_SECRET_KEY=12345", encoding="utf-8")

        self.slack_bridge: Optional[SlackHitlBridge] = None
        self.slack_admin_id = os.environ.get("FIBER_SLACK_USER_ID", "U_SLACK_ADMIN_99")

        self.set_phases([
            Phase("Phase 1: Connect Spec Resolver & Dual Mode (TOTP / Button) Guard Arming", self.phase_resolver_and_guard_arming),
            Phase("Phase 2: Safe Call Passthrough (Zero-Overhead on Non-Matching Payload)", self.phase_safe_passthrough),
            Phase("Phase 3: [Network/Multiplex + TOTP] Context7 Trap & TOTP Attested Resume", self.phase_network_multiplex_totp_hitl),
            Phase("Phase 4: [Stdio/Ephemeral + 1-Click Button] Zero-Spawn Trap & Button-Only Resume", self.phase_stdio_ephemeral_button_hitl),
        ])

    async def setup_custom_context(self) -> None:
        await self.mock_mcp.start()
        self.crypto_ctx.provision()
        self.gw_client = self.crypto_ctx.create_gateway_client(base_url=self.local_url, default_timeout=25.0)

        self.slack_bridge = SlackHitlBridge(rpc=self.rpc)
        self.slack_bridge.register_identity_mapping(self.slack_admin_id, self.crypto_ctx.spiffe_id)
        self.slack_bridge.register_identity_mapping("*", self.crypto_ctx.spiffe_id)

        if self.slack_bridge.is_live_configured and self.slack_bridge.app_token:
            await self.slack_bridge.start_background_daemons()
            log.info(f"[HITLPipeline:LIVE] Live Slack Socket Mode Active! Target Channel: {self.slack_bridge.default_channel}")
        else:
            log.info(f"[HITLPipeline:SIM] Mapped Slack User '{self.slack_admin_id}' -> '{self.crypto_ctx.spiffe_id}' (Local Emulation Mode)")

    async def teardown_custom(self) -> None:
        if self.slack_bridge:
            await self.slack_bridge.stop_background_daemons()
        await self.crypto_ctx.aclose_clients()
        self.crypto_ctx.teardown()
        if self._injected_temp_env:
            os.environ.pop(self.env_key_name, None)
        await self.mock_mcp.stop()
        self.temp_dir.cleanup()

    async def setup_workers(self) -> None:
        self.add_connector(self.net_target_id, self.context7_url, preflight="RESTRICTED_TOPIC|DROP")
        self.add_connector(
            self.stdio_target_id,
            f"npx -y @modelcontextprotocol/server-filesystem {self.sandbox_path}",
            preflight="button:prod_secrets|DELETE|DROP",
        )

    async def _emulate_slack_webhook(
        self, target_id: str, handle_id: str, prompt_payload: Dict[str, Any], expect_totp: bool, mode_badge: str
    ) -> None:
        yield_event = {
            "event": "MCP_PHASE_YIELD",
            "handle_id": handle_id,
            "target_server_id": target_id,
            "agent": self.crypto_ctx.spiffe_id,
            "prompt": prompt_payload,
        }
        block_kit_msg = self.slack_bridge.build_yield_message_blocks(yield_event, channel="C_HITL_TEST")
        auth_slot_block = block_kit_msg["blocks"][3]
        approve_btn_val = block_kit_msg["blocks"][4]["elements"][0]["value"]

        if expect_totp:
            if auth_slot_block.get("block_id") != "fiber_totp_block":
                raise RuntimeError("Expected 'fiber_totp_block' input in Block-Kit for TOTP mode.")
            state_values = {
                "fiber_totp_block": {
                    "fiber_totp_input": {"type": "plain_text_input", "value": self.crypto_ctx.generate_totp()}
                }
            }
        else:
            if auth_slot_block.get("block_id") == "fiber_totp_block":
                raise RuntimeError("TOTP input block should NOT be rendered in 1-Click Button mode!")
            state_values = {}

        action_payload = {
            "type": "block_actions",
            "user": {"id": self.slack_admin_id, "username": "admin"},
            "actions": [{"action_id": "fiber_action_approve", "value": approve_btn_val}],
            "state": {"values": state_values},
        }
        form_body = urllib.parse.urlencode({"payload": json.dumps(action_payload)}).encode("utf-8")
        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(self.slack_bridge.slack_secret, f"v0:{ts}:".encode() + form_body, hashlib.sha256).hexdigest()

        oob_res = await self.slack_bridge.handle_http_webhook(ts, form_body, sig)
        if oob_res.get("status") != "RESUMED":
            raise RuntimeError(f"Expected OOB status RESUMED ({mode_badge}), got {oob_res}")

    async def _resolve_hitl_adaptive(
        self,
        target_id: str,
        handle_id: str,
        prompt_payload: Dict[str, Any],
        orig_payload: Dict[str, Any],
        idem_key: str,
        expect_totp: bool,
    ) -> str:
        actual_require_totp = bool(prompt_payload.get("params", {}).get("require_totp", True))
        if actual_require_totp != expect_totp:
            raise RuntimeError(f"Prompt require_totp mismatch on {target_id}: expected {expect_totp}, got {actual_require_totp}")

        mode_badge = "TOTP-6DIGIT" if expect_totp else "1-CLICK BUTTON"
        is_live = bool(self.slack_bridge.is_live_configured and self.slack_bridge.app_token)

        if is_live:
            await asyncio.sleep(0.4)
            print(
                f"\n{'=' * 80}\n[LIVE SLACK PRE-FLIGHT HITL | Mode: {mode_badge}] Target: {target_id}\n"
                f"   • Target Channel : {self.slack_bridge.default_channel}  |  Handle : {handle_id}\n{'-' * 80}"
            )
            start_wait = time.time()

            async def spinner_task() -> None:
                frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
                tick = 0
                while True:
                    elapsed = time.time() - start_wait
                    icon = frames[tick % len(frames)]
                    if expect_totp:
                        rem_sec = 30 - (int(time.time()) % 30)
                        msg = f"{icon} ⏳ [PARKED: TOTP] Enter TOTP [ \033[1;32m{self.crypto_ctx.generate_totp()}\033[0m ] (renews in {rem_sec:02d}s) in Slack... ({elapsed:.1f}s)"
                    else:
                        msg = f"{icon} [PARKED: 1-CLICK] Click [ \033[1;36mApprove & Resume\033[0m ] button in Slack (No TOTP required)... ({elapsed:.1f}s)"
                    sys.stdout.write(f"\r\033[K   {msg}")
                    sys.stdout.flush()
                    tick += 1
                    await asyncio.sleep(0.2)

            spinner = asyncio.create_task(spinner_task())
            try:
                event_data = await self.wait_for_intent_reply(handle_id, timeout=60.0)
            except asyncio.TimeoutError:
                print("\n" + "=" * 80)
                raise RuntimeError("Timed out waiting for live Slack approval click (60s).")
            finally:
                spinner.cancel()
                await asyncio.gather(spinner, return_exceptions=True)

            if event_data.get("status") == "RESOLVED":
                sys.stdout.write(f"\r\033[K   [SIGNAL RECEIVED] Slack {mode_badge} Verified & Pre-Flight Unlocked! ({time.time() - start_wait:.1f}s)\n{'=' * 80}\n\n")
                sys.stdout.flush()
            else:
                err_detail = event_data.get("error_detail", "Unknown")
                sys.stdout.write(f"\r\033[K   [ABORTED] Execution Denied or Faulted: {err_detail}\n{'=' * 80}\n\n")
                sys.stdout.flush()
                raise RuntimeError(f"Execution faulted on {target_id}: {err_detail}")
        else:
            try:
                event_data = await self.wait_for_intent_reply(
                    handle_id,
                    timeout=15.0,
                    trigger_coro=lambda: self._emulate_slack_webhook(target_id, handle_id, prompt_payload, expect_totp, mode_badge),
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Handle {handle_id} on {target_id} did not resolve within timeout (AnchorStore Consensus stalled).")

            if event_data.get("status") != "RESOLVED":
                raise RuntimeError(f"Handle {handle_id} FAULTED after RESUME ({mode_badge}): {event_data.get('error_detail', 'Unknown')}")
            log.info(f"  ├─ [Boundary 1] AnchorStore Consensus verified via PubSub ({handle_id}).")

        res_final = await self.gw_client.post_gateway(target_id, orig_payload, idem_key=idem_key, timeout=25.0)
        if res_final.status_code == 200:
            log.info("  ├─ [Boundary 2] External Sandbox Execution completed.")
            return McpPayloadBuilder.extract_data(res_final.content, parse_json=False)

        raise RuntimeError(f"Expected 200 OK after resolution, got {res_final.status_code}: {res_final.text}")

    # Phase 1: resolve_connector_spec 추론 및 Dual Mode(TOTP / Button) 장착 검증
    async def phase_resolver_and_guard_arming(self) -> None:
        net_conn = self._get_connector(self.net_target_id)
        stdio_conn = self._get_connector(self.stdio_target_id)

        if (
            not net_conn
            or net_conn.transport_type != "network"
            or net_conn.mode != "multiplex"
            or not net_conn.fsm.preflight.guard_regex
            or net_conn.fsm.preflight.require_totp is not True
        ):
            raise RuntimeError("Network connector failed to arm as network/multiplex with TOTP mode (require_totp=True).")

        if (
            not stdio_conn
            or stdio_conn.transport_type != "stdio"
            or stdio_conn.mode != "ephemeral"
            or not stdio_conn.fsm.preflight.guard_regex
            or stdio_conn.fsm.preflight.require_totp is not False
        ):
            raise RuntimeError("Stdio connector failed to arm as stdio/ephemeral with Button mode (require_totp=False).")

        log.info(
            f"  └─ Guard Armed: [{self.net_target_id} -> NETWORK/MULTIPLEX (Mode: TOTP)] & "
            f"[{self.stdio_target_id} -> STDIO/EPHEMERAL (Mode: 1-CLICK BUTTON)]"
        )

    # Phase 2: 정규식 미매칭(Safe) 요청의 무정지 통과(200 OK) 검증
    async def phase_safe_passthrough(self) -> None:
        safe_file = str(self.sandbox_path / "public_readme.txt")
        payload = self.mcp.next_tool_call("read_file", path=safe_file)
        res = await self.gw_client.post_gateway(self.stdio_target_id, payload)
        if res.status_code != 200:
            raise RuntimeError(f"Safe call should return 200 OK immediately, got {res.status_code}")

        content = McpPayloadBuilder.extract_data(res.content, parse_json=False)
        if "SAFE_PUBLIC_CONTENT" not in content:
            raise RuntimeError(f"Unexpected file content: {content}")

        log.info("  └─ Safe Stdio Call ('public_readme.txt') bypassed Pre-Flight Guard with 200 OK.")

    # Phase 3: [Network/Multiplex + TOTP 모드] Context7 차단 및 TOTP 검증 재개
    async def phase_network_multiplex_totp_hitl(self) -> None:
        net_conn = self._get_connector(self.net_target_id)
        idem_key = uuid.uuid4().hex
        payload = self.mcp.next_tool_call(
            "query-docs",
            context7CompatibleLibraryID="/duckdb/duckdb",
            libraryId="/duckdb/duckdb",
            libraryName="duckdb",
            topic="RESTRICTED_TOPIC",
            query="RESTRICTED_TOPIC",
        )

        res_yield = await self.gw_client.post_gateway(self.net_target_id, payload, idem_key=idem_key)
        if res_yield.status_code != 202:
            raise RuntimeError(f"Expected 202 YIELD on Context7 preflight, got {res_yield.status_code}")

        handle_id = self.gw_client.extract_handle_id(res_yield)
        stashed = net_conn.fsm.preflight.stashed_payloads.get(handle_id)
        if not stashed or stashed.get("require_totp") is not True:
            raise RuntimeError("Context7 payload was not stashed with require_totp=True.")

        log.info(f"  ├─ [Network/Multiplex | TOTP] Trapped 'RESTRICTED_TOPIC' prior to HTTP I/O (Handle: {handle_id})")

        content = await self._resolve_hitl_adaptive(
            self.net_target_id, handle_id, res_yield.json(), payload, idem_key, expect_totp=True
        )
        log.info(f"  └─ [Network/Multiplex | TOTP] 6-Digit TOTP Verified -> Context7 Stream Resumed ({len(content)} bytes)")

    # Phase 4: [Stdio/Ephemeral + 1-Click Button 모드] Zero-Spawn 차단 및 버튼 단독 승인 재개
    async def phase_stdio_ephemeral_button_hitl(self) -> None:
        stdio_conn = self._get_connector(self.stdio_target_id)
        idem_key = uuid.uuid4().hex
        secret_file = str(self.sandbox_path / "prod_secrets.env")
        payload = self.mcp.next_tool_call("read_file", path=secret_file)

        res_yield = await self.gw_client.post_gateway(self.stdio_target_id, payload, idem_key=idem_key)
        if res_yield.status_code != 202:
            raise RuntimeError(f"Expected 202 YIELD on Stdio preflight, got {res_yield.status_code}")

        handle_id = self.gw_client.extract_handle_id(res_yield)
        if handle_id in stdio_conn.active_sandboxes or len(stdio_conn.active_sandboxes) > 0:
            raise RuntimeError("SECURITY VIOLATION: npx process was spawned before Pre-Flight Button approval!")

        stashed = stdio_conn.fsm.preflight.stashed_payloads.get(handle_id)
        if not stashed or stashed.get("require_totp") is not False:
            raise RuntimeError("Stdio payload was not stashed with require_totp=False.")

        log.info(f"  ├─ [Stdio/Ephemeral | 1-Click] Zero-Spawn Confirmed (active_sandboxes=0 while parked at {handle_id})")

        content = await self._resolve_hitl_adaptive(
            self.stdio_target_id, handle_id, res_yield.json(), payload, idem_key, expect_totp=False
        )

        if "SUPER_SECRET_KEY=12345" not in content:
            raise RuntimeError(f"Expected secret file content after button approval, got: {content}")

        if handle_id in stdio_conn.active_sandboxes:
            raise RuntimeError("Ephemeral sandbox was not cleaned up after resolution.")

        log.info("  └─ [Stdio/Ephemeral | 1-Click] Button-Only Approved -> Lazy npx Spawn -> File Read -> Sandbox Destroyed!")


async def run_suite() -> None:
    pipeline = ThirdPartyPreflightHitlPipeline(
        E2EConfig(host="127.0.0.1", port=get_free_port(DEFAULT_GATEWAY_PORT), protocol="http")
    )
    results: List[TestResult] = await pipeline.run_pipeline()
    all_passed = all(r.passed for r in results)

    lines = ["\n" + "=" * 90, "[3RD-PARTY ZERO-CODE PRE-FLIGHT HITL (TOTP & 1-CLICK BUTTON) E2E REPORT]", "=" * 90]
    for idx, r in enumerate(results, 1):
        lines.append(
            f"{'[O]' if r.passed else '[X]'} {idx:02d}. [{r.target}]".ljust(22)
            + f"{r.scenario.ljust(60)} | {'PASSED' if r.passed else 'FAILED'}"
        )
    lines.extend([
        "-" * 90,
        "PRE-FLIGHT HITL PIPELINE VALIDATED SUCCESSFULLY." if all_passed else "PRE-FLIGHT HITL FAILURE DETECTED.",
        "=" * 90 + "\n",
    ])
    print("\n".join(lines), flush=True)


def main(args_list: Optional[List[str]] = None) -> None:
    PhaseReactor.ignite(main_coro_func=run_suite)


if __name__ == "__main__":
    main()