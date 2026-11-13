# fiber.gateway.bridge.fsm.execution
import json
import uuid
from typing import Dict, Any, Optional, Tuple, List
import httpx

from fiber.llm.entry import acompletion
from fiber.llm.pipeline.channel.breaker import StreamBreaker
from fiber.llm.pipeline.channel.observer import MeteringObserver
from fiber.node.finops.watcher import default_finops_watcher, default_tenant_eco

from xphi.arch.bound.adapter.gateway import DPoPClientGenerator
from fiber.node.auth.payload import NodeAuthPayload
from fiber.gateway.bridge.emitter import get_emitter
from xphi.arch.contract.fsm import (
    CompactKey,
    NodeType,
    apply_projection,
    extract_json_path,
    parse_resource_uri,
    resolve_dynamic_params,
)
from xphi.watcher.reflex.controller import BranchCursor, BranchController

log = get_emitter("fsm.bridge.execution", phase="ACTUATOR_BRIDGE")


class FiberExecutionBridge:
    def __init__(
        self,
        runtime: Optional[BranchController] = None,
        tenant_id: str = "default_tenant",
        mcp_gateway_url: str = "http://127.0.0.1:8356/v1/mcp-gateway",
        interceptors: Optional[List[Any]] = None,
        http_timeout: float = 35.0,
    ):
        self.runtime = runtime
        self.tenant_id = tenant_id
        self.mcp_gateway_url = mcp_gateway_url.rstrip("/")
        self.interceptors = interceptors or []
        self._http_client = httpx.AsyncClient(timeout=http_timeout)
        self._dpop = DPoPClientGenerator(key_size=2048)

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def __call__(
        self,
        cursor: BranchCursor,
        node_spec: Dict[str, Any],
        task_io: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
        node_type = node_spec.get(CompactKey.TYPE)

        # Pure Control / Terminal Nodes: Zero external I/O, zero fuel cost
        if node_type in (NodeType.ROUTER.value, NodeType.ACT.value):
            return {}, None, 0

        # LLM Inference & Cognitive Nodes -> Fiber Drop-in LLM Pipeline
        if node_type in (
            NodeType.PROJECTOR.value,
            NodeType.REFLECT.value,
            NodeType.TENSION.value,
        ):
            return await self._execute_llm_node(cursor, task_io)

        # External Tool / Sidecar Nodes -> Fiber MCP Gateway
        if node_type in (NodeType.OPERSIG.value, NodeType.PENDING.value):
            return await self._execute_mcp_node(cursor, task_io)

        return {}, None, 1

    # [Actuator] Fiber LLM Pipeline + StreamBreaker + VCR + Projection
    async def _execute_llm_node(
        self,
        cursor: BranchCursor,
        task_io: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
        parsed_uri = parse_resource_uri(task_io.get("resource_uri", ""))
        call_params = resolve_dynamic_params(task_io.get("call_params", {}), cursor.meta_ctx)
        vcr_cfg = task_io.get("vcr") or {}
        projection = task_io.get("projection")

        # 1. Resolve `<provider>/<model>` from Parameters or URI (`llm://openai/gpt-4o`)
        model = call_params.pop("model", None) or parsed_uri.model_hint or "auto"

        # 2. Convert shorthand ASL `"prompt"` (and optional `"system"`) into OpenAI `"messages"`
        prompt_text = call_params.pop("prompt", None)
        system_text = call_params.pop("system", None)
        messages = call_params.pop("messages", None)
        if not messages:
            messages = []
            if system_text:
                messages.append({"role": "system", "content": str(system_text)})
            user_content = str(prompt_text) if prompt_text is not None else json.dumps(cursor.meta_ctx, default=str)
            messages.append({"role": "user", "content": user_content})

        stream = bool(call_params.pop("stream", False))

        auth_payload = NodeAuthPayload(
            tenant_id=self.tenant_id,
            fuel_budget=cursor.fuel,
            is_enforced=True,
        )
        pta_adapter = self.runtime.pta if self.runtime is not None else None

        response = await acompletion(
            model=model,
            messages=messages,
            stream=stream,
            interceptors=self.interceptors,
            stream_breaker=StreamBreaker(finops_watcher=default_finops_watcher),
            metering_observer=MeteringObserver(tenant_eco=default_tenant_eco, pta_adapter=pta_adapter),
            metadata={
                "node_auth": auth_payload,
                "checkpoint_handle": cursor.pointer.to_key(),
                "vcr_scenario": f"{cursor.stream_id}_{cursor.curr_node}_{cursor.tick}",
                **vcr_cfg,
            },
            **call_params,
        )

        raw_dump = await self._normalize_llm_response(response, stream=stream)
        tokens_used = extract_json_path(raw_dump, "usage.total_tokens") or 10

        meta_updates, offload_payload = apply_projection(raw_output=raw_dump, projection=projection)
        return meta_updates, offload_payload, max(1, int(tokens_used))

    # [Actuator] Fiber MCP Gateway + Idempotent Checkpoint Handle + HITL
    async def _execute_mcp_node(
        self,
        cursor: BranchCursor,
        task_io: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
        raw_uri = str(task_io.get("resource_uri", ""))
        parsed_uri = parse_resource_uri(raw_uri)
        resolved_args = resolve_dynamic_params(task_io.get("call_params", {}), cursor.meta_ctx)
        projection = task_io.get("projection")
        extras = task_io.get("extras") or {}

        # 1. Resolve `target_id` and `tool_name` for both `mcp://target_id/tool_name` and `mcp://target_id`
        target_id, uri_tool = self._split_mcp_target_and_tool(raw_uri, parsed_uri)
        tool_name = resolved_args.pop("_tool_name", None) or uri_tool

        receipt = str(resolved_args.pop("_fuel_receipt", "valid_receipt"))
        idem_key = self._build_idempotency_key(cursor, resolved_args)
        ckpt_handle = cursor.pointer.to_key()

        url = f"{self.mcp_gateway_url}/{target_id}/invoke"
        nonce = uuid.uuid4().hex
        headers = {
            "x-idempotency-key": idem_key,
            "x-nonce": nonce,
            "X-Fiber-Fuel-Receipt": receipt,
            "X-Fiber-Checkpoint": ckpt_handle,
            "X-Fiber-Tenant": self.tenant_id,
            "X-Fiber-Stream": cursor.stream_id,
            "DPoP": self._dpop.generate_proof(url=url, method="POST", nonce=nonce),
        }

        if tool_name:
            payload: Dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": int(cursor.tick or 1),
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": resolved_args},
            }
        else:
            payload = {
                "checkpoint_handle": ckpt_handle,
                "arguments": resolved_args,
                "meta": cursor.meta_ctx,
            }

        try:
            resp = await self._http_client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise TimeoutError(
                f"[Bridge:{cursor.stream_id}] MCP Gateway timeout on '{target_id}/{tool_name or ''}' ({url})"
            ) from exc

        # HTTP 202 (SEP-2322 Pre-Flight HITL Yield): Park cursor without advancing state
        if resp.status_code == 202:
            try:
                hitl_data = resp.json()
            except Exception:
                hitl_data = {"status": 202, "raw": resp.text}
            if not isinstance(hitl_data, dict):
                hitl_data = {"data": hitl_data}

            ticket = (
                hitl_data.get("ticket_id")
                or hitl_data.get("handle_id")
                or extract_json_path(hitl_data, "detail.handle_id")
                or extract_json_path(hitl_data, "params.handle_id")
                or resp.headers.get("X-Fiber-HITL-Ticket", "")
            )
            log.info(f"[Bridge:{cursor.stream_id}] MCP Pre-Flight HITL Lock triggered at '{cursor.curr_node}'.")
            return (
                {
                    "_hitl_yield": True,
                    "_hitl_ticket": str(ticket),
                    "_idem_key": idem_key,
                },
                hitl_data,
                0,
            )

        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise httpx.HTTPStatusError(
                f"MCP Gateway HTTP {resp.status_code} on '{target_id}/{tool_name or ''}': {detail}",
                request=resp.request,
                response=resp,
            )

        raw_json = resp.json()
        if isinstance(raw_json, dict) and "error" in raw_json and raw_json["error"]:
            raise RuntimeError(
                f"[Bridge:{cursor.stream_id}] MCP JSON-RPC error on '{target_id}/{tool_name or ''}': {raw_json['error']}"
            )

        tool_result = self._unwrap_mcp_jsonrpc(raw_json)

        meta_updates, offload_payload = apply_projection(
            raw_output=tool_result,
            projection=projection,
            base_meta={"intent_type": "tool_completed", "_hitl_yield": False},
        )
        default_cost = int(extras.get("step_cost", tool_result.get("step_cost", 5)))
        return meta_updates, offload_payload, max(1, default_cost)

    @staticmethod
    def _split_mcp_target_and_tool(raw_uri: str, parsed_uri: Any) -> Tuple[str, Optional[str]]:
        """Extracts `(target_id, tool_name)` reliably across `mcp://target/tool` and `mcp://target`."""
        candidate = (
            raw_uri.split("://", 1)[1].strip("/")
            if "://" in raw_uri
            else (getattr(parsed_uri, "target", None) or getattr(parsed_uri, "target_id", "")).strip("/")
        )
        if "/" in candidate:
            target_id, tool_name = candidate.split("/", 1)
            return target_id.strip(), (tool_name.strip() or None)
        return (getattr(parsed_uri, "target_id", "") or candidate).strip(), None

    @staticmethod
    def _build_idempotency_key(cursor: BranchCursor, resolved_args: Dict[str, Any]) -> str:
        """
        Generates a clean 32-char hex idempotency key unique per branch & checkpoint,
        preventing sibling collision after 1:N fork while remaining deterministic across HITL resume.
        """
        explicit = resolved_args.pop("_idem_key", None) or cursor.meta_ctx.get("_idem_key")
        if explicit:
            return str(explicit)
        seed = f"{cursor.stream_id}:{cursor.curr_node}:{cursor.pointer.to_key()}"
        return uuid.uuid5(uuid.NAMESPACE_OID, seed).hex

    @staticmethod
    def _unwrap_mcp_jsonrpc(raw_resp: Any) -> Dict[str, Any]:
        if not isinstance(raw_resp, dict):
            return {"data": raw_resp}
        if "result" in raw_resp and isinstance(raw_resp["result"], dict):
            content_list = raw_resp["result"].get("content")
            if isinstance(content_list, list) and content_list:
                first_text = content_list[0].get("text")
                if isinstance(first_text, str):
                    try:
                        parsed = json.loads(first_text)
                        if isinstance(parsed, dict):
                            return parsed
                        return {"data": parsed}
                    except Exception:
                        return {"data": first_text}
            return raw_resp["result"]
        return raw_resp

    @classmethod
    async def _normalize_llm_response(cls, response: Any, stream: bool) -> Dict[str, Any]:
        if stream and hasattr(response, "__aiter__"):
            chunks: List[str] = []
            async for chunk in response:
                if hasattr(chunk, "choices") and chunk.choices:
                    delta = getattr(chunk.choices[0], "delta", None)
                    if delta and getattr(delta, "content", None):
                        chunks.append(delta.content)
                elif isinstance(chunk, dict):
                    delta_content = extract_json_path(chunk, "choices.0.delta.content")
                    if delta_content:
                        chunks.append(str(delta_content))
            text = "".join(chunks)
            dump: Dict[str, Any] = {
                "content": text,
                "raw_stream_payload": text,
                "usage": {"total_tokens": max(1, len(chunks))},
            }
            cls._try_merge_json_content(dump, text)
            return dump

        if hasattr(response, "model_dump"):
            dump = response.model_dump()
        elif isinstance(response, dict):
            dump = dict(response)
        else:
            dump = {"raw": str(response)}

        # Surface `choices.0.message.content` to top-level `content` and `raw_stream_payload` for parity
        msg_content = extract_json_path(dump, "choices.0.message.content")
        if isinstance(msg_content, str):
            dump.setdefault("content", msg_content)
            dump.setdefault("raw_stream_payload", msg_content)
            cls._try_merge_json_content(dump, msg_content)

        return dump

    @staticmethod
    def _try_merge_json_content(target_dump: Dict[str, Any], text: str) -> None:
        """If the LLM output is a JSON object string, merges its keys into top-level dump for `extract_keys`."""
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                stripped = "\n".join(lines[1:-1]).strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed_json = json.loads(stripped)
                if isinstance(parsed_json, dict):
                    for k, v in parsed_json.items():
                        target_dump.setdefault(k, v)
            except Exception:
                pass

    # Backward-compatible static delegates for subclasses
    @staticmethod
    def _resolve_dynamic_params(params: Dict[str, Any], meta_ctx: Dict[str, Any]) -> Dict[str, Any]:
        return resolve_dynamic_params(params, meta_ctx)

    @staticmethod
    def _extract_path(data: Dict[str, Any], path: Optional[str]) -> Any:
        return extract_json_path(data, path)