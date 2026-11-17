# fiber.gateway.bridge.channel.schema
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class SelectOption:
    """Generic key-value option for interactive dropdown controls."""

    value: str
    label: str

    def to_slack_option(self) -> Dict[str, Any]:
        return {
            "text": SlackBlockBuilder.plain_text(self.label[:75]),
            "value": str(self.value),
        }

    @classmethod
    def from_raw(cls, item: Any) -> SelectOption:
        if isinstance(item, SelectOption):
            return item
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            return cls(value=str(item[0]), label=str(item[1]))
        if isinstance(item, dict):
            val = item.get("value") or item.get("code") or ""
            lbl = item.get("label") or val
            return cls(value=str(val), label=str(lbl))
        raise ValueError(f"Unsupported option format: {item}")


# 도메인 중립적인 시스템 기본 프리셋 (외부 주입이 없을 때만 사용되는 기술 표준 분류)
DEFAULT_ABORT_REASONS: Tuple[SelectOption, ...] = (
    SelectOption("POLICY_FALSE_POSITIVE", "False Positive: Guardrail pattern mismatch"),
    SelectOption("PRE_AUTHORIZED_ENTITY", "Exempt: Target resource is pre-authorized"),
    SelectOption("PARAMETER_OUT_OF_BOUNDS", "Abort: Arguments require upstream regeneration"),
    SelectOption("OPERATOR_MANUAL_VETO", "Veto: Rejected by operator policy"),
)

DEFAULT_LEASE_POLICIES: Tuple[SelectOption, ...] = (
    SelectOption("0", "Single Execution Only (No Lease)"),
    SelectOption("900", "Grant 15m Scoped Execution Lease (900s)"),
    SelectOption("3600", "Grant 1h Scoped Execution Lease (3600s)"),
)


@dataclass
class InteractiveUiLabels:
    """Configurable UI text labels. Defaults to domain-agnostic technical terminology."""

    header_title: str = "Fiber HITL: Interactive Execution Gate"
    summary_heading: str = "Intercepted Context:"
    steering_label: str = "Execution Mode / Policy Selector"
    override_single_fmt: str = "Argument Override ({key})"
    override_json_label: str = "Argument Override (JSON Payload)"
    totp_label: str = "Authenticator TOTP Code"
    totp_placeholder: str = "Enter 6-digit RFC-6238 code"
    abort_reason_label: str = "Abort Classification (Rollback Tag)"
    abort_reason_placeholder: str = "Select abort classification (if denying)"
    comment_label: str = "Operator Audit Annotation"
    comment_placeholder: str = "Optional audit note or rollback rationale"
    lease_label: str = "Scoped Session Lease (TTL)"
    approve_button: str = "Approve & Resume"
    deny_button: str = "Deny & Rollback"


@dataclass
class InteractivePromptSpec:
    """
    Normalized specification parsed from `yield_event['prompt']['params']`.
    Allows external applications to inject custom options and labels per invocation.
    """

    summary_text: str = "High-risk tool invocation parked for operator attestation."
    require_totp: bool = False
    interaction_profile: str = "standard_ext"
    editable_args: Dict[str, Any] = field(default_factory=dict)
    steering_options: List[SelectOption] = field(default_factory=list)
    abort_reasons: List[SelectOption] = field(default_factory=list)
    lease_options: List[SelectOption] = field(default_factory=list)
    allow_comment: bool = True
    labels: InteractiveUiLabels = field(default_factory=InteractiveUiLabels)

    @property
    def override_keys(self) -> List[str]:
        return list(self.editable_args.keys())

    @staticmethod
    def has_interactive_params(params: Dict[str, Any]) -> bool:
        trigger_keys = {
            "interaction_profile",
            "editable_args",
            "steering_options",
            "abort_reasons",
            "lease_options",
            "allow_comment",
            "summary_text",
            "ui_labels",
        }
        return bool(trigger_keys.intersection(params.keys()))

    @classmethod
    def from_prompt_params(
        cls,
        params: Dict[str, Any],
        default_abort_reasons: Tuple[SelectOption, ...] = DEFAULT_ABORT_REASONS,
        default_lease_options: Tuple[SelectOption, ...] = DEFAULT_LEASE_POLICIES,
        default_labels: Optional[InteractiveUiLabels] = None,
    ) -> InteractivePromptSpec:
        base_labels = default_labels or InteractiveUiLabels()
        raw_labels = params.get("ui_labels")
        if isinstance(raw_labels, dict):
            valid_keys = InteractiveUiLabels.__dataclass_fields__.keys()
            filtered = {k: str(v) for k, v in raw_labels.items() if k in valid_keys}
            merged_labels = InteractiveUiLabels(**{**base_labels.__dict__, **filtered})
        else:
            merged_labels = base_labels

        steering = [SelectOption.from_raw(x) for x in params.get("steering_options", [])]

        raw_abort = params.get("abort_reasons", True)
        if isinstance(raw_abort, list):
            aborts = [SelectOption.from_raw(x) for x in raw_abort]
        elif raw_abort:
            aborts = list(default_abort_reasons)
        else:
            aborts = []

        raw_lease = params.get("lease_options", False)
        if isinstance(raw_lease, list):
            leases = [SelectOption.from_raw(x) for x in raw_lease]
        elif raw_lease:
            leases = list(default_lease_options)
        else:
            leases = []

        return cls(
            summary_text=str(
                params.get("summary_text")
                or params.get("message")
                or "High-risk tool invocation parked for operator attestation."
            ),
            require_totp=bool(params.get("require_totp", False)),
            interaction_profile=str(params.get("interaction_profile", "standard_ext")),
            editable_args=dict(params.get("editable_args") or {}),
            steering_options=steering,
            abort_reasons=aborts,
            lease_options=leases,
            allow_comment=bool(params.get("allow_comment", True)),
            labels=merged_labels,
        )


@dataclass
class ExtractedInteraction:
    """Strongly-typed representation of a parsed operator interaction before OOB dispatch."""

    state_token: Dict[str, Any]
    slack_user_id: str
    decision: str
    otp_code: str
    response_url: Optional[str] = None
    override_args: Optional[Dict[str, Any]] = None
    comment: Optional[str] = None
    reason: Optional[str] = None
    lease_ttl_sec: Optional[int] = None

    def to_normalized_dict(self) -> Dict[str, Any]:
        """Converts to the exact dictionary contract expected by `OutOfBandApprovalBridge`."""
        out: Dict[str, Any] = {
            "state_token": self.state_token,
            "slack_user_id": self.slack_user_id,
            "decision": self.decision,
            "otp_code": self.otp_code,
            "response_url": self.response_url,
        }
        if self.override_args is not None:
            out["override_args"] = self.override_args
        if self.comment is not None:
            out["comment"] = self.comment
        if self.reason is not None:
            out["reason"] = self.reason
        if self.lease_ttl_sec is not None:
            out["lease_ttl_sec"] = self.lease_ttl_sec
        return out


class OverrideCodec:
    """Encapsulates JSON/Text serialization and parsing for in-flight argument overrides."""

    @staticmethod
    def encode_compact_token(token_dict: Dict[str, Any]) -> str:
        return json.dumps(token_dict, separators=(",", ":"))

    @staticmethod
    def format_initial_override(
        editable_args: Dict[str, Any], labels: InteractiveUiLabels
    ) -> Tuple[str, str]:
        keys = list(editable_args.keys())
        if len(keys) == 1 and isinstance(editable_args[keys[0]], str):
            only_key = keys[0]
            return editable_args[only_key], labels.override_single_fmt.format(key=only_key)
        return (
            json.dumps(editable_args, ensure_ascii=False, indent=2),
            labels.override_json_label,
        )

    @staticmethod
    def parse_override_input(
        raw_text: Optional[str], override_keys: List[str]
    ) -> Optional[Dict[str, Any]]:
        if raw_text is None or not raw_text.strip():
            return None
        cleaned = raw_text.strip()
        if len(override_keys) == 1 and not cleaned.startswith("{"):
            return {override_keys[0]: cleaned}
        try:
            parsed = json.loads(cleaned)
            if not isinstance(parsed, dict):
                raise ValueError("Override payload must deserialize to a JSON object.")
            return parsed
        except Exception as exc:
            raise ValueError(f"Malformed override_args payload: {exc}") from exc


class SlackBlockBuilder:
    """Pure builder methods to eliminate repetitive Slack Block Kit dictionary literals."""

    @staticmethod
    def plain_text(text: str) -> Dict[str, Any]:
        return {"type": "plain_text", "text": text}

    @staticmethod
    def mrkdwn(text: str) -> Dict[str, Any]:
        return {"type": "mrkdwn", "text": text}

    @classmethod
    def header(cls, title: str) -> Dict[str, Any]:
        return {"type": "header", "text": cls.plain_text(title[:150])}

    @classmethod
    def metadata_section(
        cls, target_id: str, node_fp: str, handle_id: str, agent_uri: str
    ) -> Dict[str, Any]:
        return {
            "type": "section",
            "fields": [
                cls.mrkdwn(f"*Target MCP:*\n`{target_id}`"),
                cls.mrkdwn(f"*Node Fingerprint:*\n`{node_fp}`"),
                cls.mrkdwn(f"*Handle ID:*\n`{handle_id}`"),
                cls.mrkdwn(f"*Requester:*\n`{agent_uri}`"),
            ],
        }

    @classmethod
    def summary_section(cls, heading: str, body: str) -> Dict[str, Any]:
        return {"type": "section", "text": cls.mrkdwn(f"*{heading}*\n{body}")}

    @classmethod
    def text_input_block(
        cls,
        block_id: str,
        action_id: str,
        label: str,
        *,
        optional: bool = True,
        multiline: bool = False,
        initial_value: Optional[str] = None,
        placeholder: Optional[str] = None,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
    ) -> Dict[str, Any]:
        element: Dict[str, Any] = {
            "type": "plain_text_input",
            "action_id": action_id,
            "multiline": multiline,
        }
        if initial_value is not None:
            element["initial_value"] = initial_value[:3000]
        if placeholder is not None:
            element["placeholder"] = cls.plain_text(placeholder[:150])
        if min_length is not None:
            element["min_length"] = min_length
        if max_length is not None:
            element["max_length"] = max_length

        return {
            "type": "input",
            "block_id": block_id,
            "optional": optional,
            "label": cls.plain_text(label[:75]),
            "element": element,
        }

    @classmethod
    def select_input_block(
        cls,
        block_id: str,
        action_id: str,
        label: str,
        options: List[SelectOption],
        *,
        optional: bool = True,
        select_first_by_default: bool = False,
        placeholder: Optional[str] = None,
    ) -> Dict[str, Any]:
        slack_opts = [opt.to_slack_option() for opt in options]
        element: Dict[str, Any] = {
            "type": "static_select",
            "action_id": action_id,
            "options": slack_opts,
        }
        if select_first_by_default and slack_opts:
            element["initial_option"] = slack_opts[0]
        elif placeholder:
            element["placeholder"] = cls.plain_text(placeholder[:150])

        return {
            "type": "input",
            "block_id": block_id,
            "optional": optional,
            "label": cls.plain_text(label[:75]),
            "element": element,
        }

    @classmethod
    def decision_actions_block(
        cls, token_str: str, approve_label: str, deny_label: str
    ) -> Dict[str, Any]:
        return {
            "type": "actions",
            "block_id": "fiber_decision_block",
            "elements": [
                {
                    "type": "button",
                    "text": cls.plain_text(approve_label[:75]),
                    "style": "primary",
                    "action_id": "fiber_action_approve",
                    "value": token_str,
                },
                {
                    "type": "button",
                    "text": cls.plain_text(deny_label[:75]),
                    "style": "danger",
                    "action_id": "fiber_action_deny",
                    "value": token_str,
                },
            ],
        }


class SlackStateExtractor:
    """Helper to read deeply nested `state.values` from Slack block_actions payloads."""

    def __init__(self, slack_payload: Dict[str, Any]):
        self._values: Dict[str, Any] = slack_payload.get("state", {}).get("values", {})

    def get_text(self, block_id: str, action_id: str) -> Optional[str]:
        val = self._values.get(block_id, {}).get(action_id, {}).get("value")
        return val.strip() if isinstance(val, str) and val.strip() else None

    def get_selected_value(self, block_id: str, action_id: str) -> Optional[str]:
        opt = self._values.get(block_id, {}).get(action_id, {}).get("selected_option")
        if isinstance(opt, dict) and opt.get("value") is not None:
            return str(opt["value"])
        return None