# fiber.gateway.bridge.channel.extoob
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from fiber.gateway.bridge.channel.oob import ApprovalStateEnvelope
from fiber.gateway.bridge.channel.schema import (
    DEFAULT_ABORT_REASONS,
    DEFAULT_LEASE_POLICIES,
    ExtractedInteraction,
    InteractivePromptSpec,
    InteractiveUiLabels,
    OverrideCodec,
    SelectOption,
    SlackBlockBuilder,
    SlackStateExtractor,
)
from fiber.gateway.bridge.channel.slack import SlackHitlBridge
from fiber.gateway.bridge.emitter import get_emitter
from xphi.arch.bound.client.rpc import InternalRpcClient

log = get_emitter("bridge.channel.extoob")


class ExtendedOobBridge(SlackHitlBridge):
    """
    Domain-agnostic interactive extension of `SlackHitlBridge`.
    Supports execution steering, in-flight argument overrides, structured abort tags,
    and scoped session leases while delegating wire formatting to `schema.py`.
    """

    # Block & Action IDs
    BLK_STEERING = "fiber_ext_steering_block"
    ACT_STEERING = "fiber_ext_steering_select"

    BLK_OVERRIDE = "fiber_ext_override_block"
    ACT_OVERRIDE = "fiber_ext_override_input"

    BLK_ABORT = "fiber_ext_abort_block"
    ACT_ABORT = "fiber_ext_abort_select"

    BLK_COMMENT = "fiber_ext_comment_block"
    ACT_COMMENT = "fiber_ext_comment_input"

    BLK_LEASE = "fiber_ext_lease_block"
    ACT_LEASE = "fiber_ext_lease_select"

    def __init__(
        self,
        rpc: Optional[InternalRpcClient] = None,
        bot_token: Optional[str] = None,
        app_token: Optional[str] = None,
        default_channel: Optional[str] = None,
        slack_signing_secret: Optional[str] = None,
        *,
        default_abort_reasons: Tuple[SelectOption, ...] = DEFAULT_ABORT_REASONS,
        default_lease_options: Tuple[SelectOption, ...] = DEFAULT_LEASE_POLICIES,
        default_labels: Optional[InteractiveUiLabels] = None,
    ):
        super().__init__(
            rpc=rpc,
            bot_token=bot_token,
            app_token=app_token,
            default_channel=default_channel,
            slack_signing_secret=slack_signing_secret,
        )
        self.default_abort_reasons = default_abort_reasons
        self.default_lease_options = default_lease_options
        self.default_labels = default_labels or InteractiveUiLabels()

    def mint_extended_state_token(
        self,
        handle_id: str,
        target_id: str,
        prompt_id: str,
        spec: InteractivePromptSpec,
        ttl_sec: int = 300,
    ) -> Dict[str, Any]:
        self._owned_handles.add(handle_id)
        extra: Dict[str, Any] = {"interaction_profile": spec.interaction_profile}
        if spec.override_keys:
            extra["override_keys"] = spec.override_keys

        env_model = ApprovalStateEnvelope(
            handle_id=handle_id,
            target_id=target_id,
            prompt_id=prompt_id,
            exp=int(time.time()) + ttl_sec,
            node_fp=self.node_fp,
            bridge_id=self.bridge_id,
            require_totp=spec.require_totp,
            extra=extra,
        )
        sig = self.signer.sign_payload(env_model.to_canonical_bytes())
        return {"envelope": env_model.to_dict(), "sig": sig}

    def build_yield_message_blocks(
        self, yield_event: Dict[str, Any], channel: Optional[str] = None
    ) -> Dict[str, Any]:
        prompt_obj = yield_event.get("prompt", {})
        prompt_params = prompt_obj.get("params", {})

        # 확장 파라미터가 없는 일반 요청은 부모(SlackHitlBridge) 로직으로 100% 동일하게 처리
        if not InteractivePromptSpec.has_interactive_params(prompt_params):
            return super().build_yield_message_blocks(yield_event, channel=channel)

        spec = InteractivePromptSpec.from_prompt_params(
            prompt_params,
            default_abort_reasons=self.default_abort_reasons,
            default_lease_options=self.default_lease_options,
            default_labels=self.default_labels,
        )

        handle_id = yield_event["handle_id"]
        target_id = yield_event["target_server_id"]
        agent_uri = yield_event.get("agent", "spiffe://public/agent")
        prompt_id = prompt_obj.get("id", f"prompt_{handle_id}")

        state_token = self.mint_extended_state_token(
            handle_id=handle_id,
            target_id=target_id,
            prompt_id=prompt_id,
            spec=spec,
        )
        token_str = OverrideCodec.encode_compact_token(state_token)

        blocks: List[Dict[str, Any]] = [
            SlackBlockBuilder.header(spec.labels.header_title),
            SlackBlockBuilder.metadata_section(target_id, self.node_fp, handle_id, agent_uri),
            SlackBlockBuilder.summary_section(spec.labels.summary_heading, spec.summary_text),
        ]

        if spec.steering_options:
            blocks.append(
                SlackBlockBuilder.select_input_block(
                    self.BLK_STEERING,
                    self.ACT_STEERING,
                    spec.labels.steering_label,
                    spec.steering_options,
                    optional=False,
                    select_first_by_default=True,
                )
            )

        if spec.editable_args:
            init_val, override_lbl = OverrideCodec.format_initial_override(
                spec.editable_args, spec.labels
            )
            blocks.append(
                SlackBlockBuilder.text_input_block(
                    self.BLK_OVERRIDE,
                    self.ACT_OVERRIDE,
                    override_lbl,
                    optional=True,
                    multiline=True,
                    initial_value=init_val,
                )
            )

        if spec.require_totp:
            blocks.append(
                SlackBlockBuilder.text_input_block(
                    "fiber_totp_block",
                    "fiber_totp_input",
                    spec.labels.totp_label,
                    optional=False,
                    placeholder=spec.labels.totp_placeholder,
                    min_length=6,
                    max_length=6,
                )
            )

        if spec.abort_reasons:
            blocks.append(
                SlackBlockBuilder.select_input_block(
                    self.BLK_ABORT,
                    self.ACT_ABORT,
                    spec.labels.abort_reason_label,
                    spec.abort_reasons,
                    optional=True,
                    placeholder=spec.labels.abort_reason_placeholder,
                )
            )

        if spec.allow_comment:
            blocks.append(
                SlackBlockBuilder.text_input_block(
                    self.BLK_COMMENT,
                    self.ACT_COMMENT,
                    spec.labels.comment_label,
                    optional=True,
                    placeholder=spec.labels.comment_placeholder,
                )
            )

        if spec.lease_options:
            blocks.append(
                SlackBlockBuilder.select_input_block(
                    self.BLK_LEASE,
                    self.ACT_LEASE,
                    spec.labels.lease_label,
                    spec.lease_options,
                    optional=True,
                    select_first_by_default=True,
                )
            )

        blocks.append(
            SlackBlockBuilder.decision_actions_block(
                token_str,
                approve_label=spec.labels.approve_button,
                deny_label=spec.labels.deny_button,
            )
        )

        return {
            "channel": channel or self.default_channel,
            "text": f"[Fiber HITL] Review required for {handle_id} on {target_id}",
            "blocks": blocks,
        }

    @classmethod
    def extract_interaction(cls, slack_payload: Dict[str, Any]) -> ExtractedInteraction:
        """Parses Slack block_actions into a structured `ExtractedInteraction` dataclass."""
        base_dict = SlackHitlBridge.extract_interaction_from_block_actions(slack_payload)
        state_token = base_dict["state_token"]
        envelope = state_token.get("envelope", {})
        extractor = SlackStateExtractor(slack_payload)

        # 1. Execution Steering value (if TOTP is not required)
        otp_or_steering = base_dict["otp_code"]
        selected_steering = extractor.get_selected_value(cls.BLK_STEERING, cls.ACT_STEERING)
        if selected_steering and not envelope.get("require_totp", True):
            otp_or_steering = selected_steering

        # 2. In-flight Argument Override
        override_keys: List[str] = envelope.get("override_keys") or []
        raw_override = extractor.get_text(cls.BLK_OVERRIDE, cls.ACT_OVERRIDE)
        override_args = OverrideCodec.parse_override_input(raw_override, override_keys)

        # 3. Abort Classification & Operator Audit Comment
        abort_code = extractor.get_selected_value(cls.BLK_ABORT, cls.ACT_ABORT)
        comment = extractor.get_text(cls.BLK_COMMENT, cls.ACT_COMMENT)

        decision = base_dict["decision"]
        reason: Optional[str] = None
        if decision != "APPROVE":
            if abort_code and comment:
                reason = f"[{abort_code}] {comment}"
            elif abort_code:
                reason = f"[{abort_code}]"
            elif comment:
                reason = comment
            else:
                reason = "OPERATOR_ABORTED"
        elif abort_code and not comment:
            comment = f"Tag: {abort_code}"

        # 4. Session Lease TTL
        lease_ttl_sec: Optional[int] = None
        raw_lease = extractor.get_selected_value(cls.BLK_LEASE, cls.ACT_LEASE)
        if raw_lease and raw_lease.isdigit() and int(raw_lease) > 0:
            lease_ttl_sec = int(raw_lease)

        return ExtractedInteraction(
            state_token=state_token,
            slack_user_id=base_dict["slack_user_id"],
            decision=decision,
            otp_code=otp_or_steering,
            response_url=base_dict.get("response_url"),
            override_args=override_args,
            comment=comment,
            reason=reason,
            lease_ttl_sec=lease_ttl_sec,
        )

    @staticmethod
    def extract_interaction_from_block_actions(slack_payload: Dict[str, Any]) -> Dict[str, Any]:
        if "state_token" in slack_payload and "slack_user_id" in slack_payload:
            return slack_payload
        return ExtendedOobBridge.extract_interaction(slack_payload).to_normalized_dict()

    def build_resolution_receipt_blocks(
        self, normalized: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        receipt = super().build_resolution_receipt_blocks(normalized, result)
        extra_lines: List[str] = []

        if result.get("status") == "RESUMED":
            if normalized.get("override_args"):
                keys_fmt = ", ".join(f"`{k}`" for k in normalized["override_args"].keys())
                extra_lines.append(f"• *Payload Override:* Mutated arguments ({keys_fmt})")
            if normalized.get("lease_ttl_sec"):
                extra_lines.append(f"• *Session Lease:* Granted for `{normalized['lease_ttl_sec']}s`")
            if normalized.get("comment"):
                extra_lines.append(f"• *Audit Annotation:* _{normalized['comment']}_")
        else:
            if normalized.get("reason"):
                extra_lines.append(f"• *Abort Classification:* `{normalized['reason']}`")

        if extra_lines and len(receipt.get("blocks", [])) >= 4:
            detail_block = receipt["blocks"][3]
            if detail_block.get("type") == "section" and "text" in detail_block:
                detail_block["text"]["text"] += "\n" + "\n".join(extra_lines)

        return receipt