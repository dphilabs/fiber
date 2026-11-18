# fiber.node.reflex.bridge.polyglot
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import inspect
import json
import shlex
from typing import Any, Callable, Dict, List, Optional, Tuple
import httpx

from fiber.gateway.bridge.emitter import get_emitter
from fiber.node.reflex.bridge.execution import FiberExecutionBridge
from fiber.node.reflex.unit.loader import UnitsLoader
from xphi.arch.bound.adapter.pta import PtaPointer
from xphi.arch.contract.reflex.fsm import (
    CompactKey,
    NodeType,
    ParsedResource,
    apply_projection,
    parse_resource_uri,
    resolve_dynamic_params,
)
from xphi.watcher.reflex.controller import BranchController, BranchCursor

log = get_emitter("reflex.bridge.polyglot", phase="POLYGLOT_BRIDGE")

LocalCallable = Callable[..., Any]

class PolyglotExecutionBridge(FiberExecutionBridge):
    """
    Unified L3 Actuator Bridge supporting two sibling control planes:
    - Async Ledger/FSM Plane (`BranchController` / `GraphTraverser`) via `await bridge(cursor, node_spec, task_io)`
    - Sync Workflow Plane (`FlowEngine` / `WorkflowViewport`) via `bridge.dispatch_sync(scheme, target, payload)`
    """
    def __init__(
        self,
        runtime: Optional[BranchController] = None,
        tenant_id: str = "default_tenant",
        mcp_gateway_url: str = "http://127.0.0.1:8356/v1/mcp-gateway",
        interceptors: Optional[List[Any]] = None,
        http_timeout: float = 30.0,
        local_handlers: Optional[Dict[str, LocalCallable]] = None,
        allow_py_import: bool = True,
        allow_exec_subprocess: bool = False,
        exec_timeout: float = 30.0,
        loader: Optional[UnitsLoader] = None,
    ):
        super().__init__(
            runtime=runtime,
            tenant_id=tenant_id,
            mcp_gateway_url=mcp_gateway_url,
            interceptors=interceptors,
            http_timeout=http_timeout,
        )
        self._http_timeout = http_timeout
        self.loader = loader or UnitsLoader()
        self._fn_registry: Dict[str, LocalCallable] = {}
        for k, v in (local_handlers or {}).items():
            self.register_fn(k, v)
        self.allow_py_import = allow_py_import
        self.allow_exec_subprocess = allow_exec_subprocess
        self.exec_timeout = exec_timeout

    def register_fn(self, name: str, handler: LocalCallable) -> "PolyglotExecutionBridge":
        """Registers an in-process sync or async callable for `fn://<name>` resources."""
        parsed = parse_resource_uri(name)
        self._fn_registry[parsed.target or parsed.target_id] = handler
        return self

    # Entrypoint 1: Async Ledger / FSM Control Plane (`BranchController`)
    async def __call__(
        self,
        cursor: BranchCursor,
        node_spec: Dict[str, Any],
        task_io: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
        node_type = node_spec.get(CompactKey.TYPE)

        if node_type == NodeType.OPERSIG.value:
            parsed = parse_resource_uri(task_io.get("resource_uri", ""))
            if parsed.is_polyglot_opersig:
                return await self._execute_polyglot_node(cursor, task_io, parsed)

        return await super().__call__(cursor, node_spec, task_io)

    # Entrypoint 2: Sync Workflow Control Plane (`FlowEngine` / `Viewport`)
    def dispatch_sync(self, scheme: str, target: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        """Synchronous `DispatcherFn` adapter for `FlowEngine`."""
        # Fast-path: local `pass`, `exec`, and file-backed `py` units go directly through `UnitsLoader`
        if scheme in ("pass", "exec"):
            return self.loader.dispatch(scheme, target, payload)
        if scheme == "py":
            mod_part, sep, _ = target.rpartition(":")
            if sep and (mod_part.endswith(".py") or "/" in mod_part or not self._fn_registry):
                return self.loader.dispatch(scheme, target, payload)

        async def _invoke_on_fresh_client() -> Dict[str, Any] | None:
            cursor = BranchCursor(
                stream_id=str(payload.get("_stream_id", "flow_engine")),
                pointer=PtaPointer(tx_hash="flow_sync", output_index=0),
                active_blob_hash="flow_memory",
                curr_node=target,
                fuel=int(payload.get("_fuel", 10000)),
                owner_pubkey="local",
                meta_ctx=dict(payload),
                tick=int(payload.get("_tick", 1)),
            )
            raw_uri = target if "://" in target else f"{scheme}://{target}"
            task_io: Dict[str, Any] = {
                "resource_uri": raw_uri,
                "call_params": dict(payload),
                "projection": None,  # `FlowEngine` applies its own projection after dispatch
            }

            # Bind a fresh AsyncClient to the current event loop to prevent closed-loop errors across sync steps
            prev_client = self._http_client
            self._http_client = httpx.AsyncClient(timeout=self._http_timeout)
            try:
                if scheme == "llm":
                    meta_updates, _, _ = await self._execute_llm_node(cursor, task_io)
                    return meta_updates
                if scheme == "mcp":
                    meta_updates, _, _ = await self._execute_mcp_node(cursor, task_io)
                    return meta_updates
                parsed = parse_resource_uri(raw_uri)
                meta_updates, _, _ = await self._execute_polyglot_node(cursor, task_io, parsed)
                return meta_updates
            finally:
                await self._http_client.aclose()
                self._http_client = prev_client

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_invoke_on_fresh_client())

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_invoke_on_fresh_client())).result()

    # Polyglot Actuator Implementations
    async def _execute_polyglot_node(
        self,
        cursor: BranchCursor,
        task_io: Dict[str, Any],
        parsed: ParsedResource,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
        resolved_args = resolve_dynamic_params(task_io.get("call_params", {}), cursor.meta_ctx)
        projection = task_io.get("projection")
        extras = task_io.get("extras") or {}
        ckpt_handle = cursor.pointer.to_key()

        if parsed.scheme == "fn":
            raw_result = await self._invoke_registered_fn(parsed, resolved_args, cursor, ckpt_handle)
        elif parsed.scheme == "py":
            raw_result = await self._invoke_dynamic_py(parsed, resolved_args, cursor, ckpt_handle)
        elif parsed.scheme in ("http", "https"):
            raw_result = await self._invoke_direct_http(parsed, resolved_args, cursor, ckpt_handle, extras)
        elif parsed.scheme == "exec":
            raw_result = await self._invoke_subprocess(parsed, resolved_args, cursor, ckpt_handle, extras)
        else:
            return await super()._execute_mcp_node(cursor, task_io)

        return self._finalize_output(cursor, raw_result, projection, extras)

    # In-Process Registered Function (`fn://<handler_name>`)
    async def _invoke_registered_fn(
        self,
        parsed: ParsedResource,
        resolved_args: Dict[str, Any],
        cursor: BranchCursor,
        ckpt_handle: str,
    ) -> Dict[str, Any]:
        handler = self._fn_registry.get(parsed.target) or self._fn_registry.get(parsed.target_id)
        if handler is None:
            raise KeyError(f"[PolyglotBridge:{cursor.stream_id}] Unregistered handler '{parsed.raw_uri}'")
        return await self._call_flexible_signature(handler, resolved_args, cursor.meta_ctx, ckpt_handle)

    # Dynamic Python Module or `fiber-units` Script (`py://<path_or_module>:<callable_name>`)
    async def _invoke_dynamic_py(
        self,
        parsed: ParsedResource,
        resolved_args: Dict[str, Any],
        cursor: BranchCursor,
        ckpt_handle: str,
    ) -> Dict[str, Any]:
        if not self.allow_py_import:
            raise PermissionError(f"[PolyglotBridge:{cursor.stream_id}] Dynamic 'py://' import is disabled.")

        target = parsed.target
        mod_part, sep, _ = target.rpartition(":")
        if sep and (mod_part.endswith(".py") or "/" in mod_part):
            out = await asyncio.to_thread(self.loader.run_py, target, resolved_args)
            return out if isinstance(out, dict) else {"data": out}

        if ":" in target:
            mod_path, attr_name = target.split(":", 1)
        elif "." in target:
            mod_path, attr_name = target.rsplit(".", 1)
        else:
            raise ValueError(f"[PolyglotBridge] Invalid 'py://' URI '{parsed.raw_uri}'. Expected 'py://module:func'.")

        module = importlib.import_module(mod_path)
        handler = getattr(module, attr_name)
        return await self._call_flexible_signature(handler, resolved_args, cursor.meta_ctx, ckpt_handle)

    # Direct REST / Webhook Server (`http://<url>` or `https://<url>`)
    async def _invoke_direct_http(
        self,
        parsed: ParsedResource,
        resolved_args: Dict[str, Any],
        cursor: BranchCursor,
        ckpt_handle: str,
        extras: Dict[str, Any],
    ) -> Dict[str, Any]:
        method = str(extras.get("method", resolved_args.pop("_http_method", "POST"))).upper()
        custom_headers = extras.get("headers") or resolved_args.pop("_http_headers", {})
        headers = {
            "X-Fiber-Checkpoint": ckpt_handle,
            "X-Fiber-Tenant": self.tenant_id,
            "X-Fiber-Stream": cursor.stream_id,
            **custom_headers,
        }

        if method in ("GET", "DELETE"):
            resp = await self._http_client.request(method, parsed.raw_uri, params=resolved_args, headers=headers)
        else:
            resp = await self._http_client.request(method, parsed.raw_uri, json=resolved_args, headers=headers)

        if resp.status_code == 202:
            try:
                hitl_data = resp.json()
            except Exception:
                hitl_data = {"status": 202, "raw": resp.text}
            if not isinstance(hitl_data, dict):
                hitl_data = {"data": hitl_data}
            hitl_data["_hitl_yield"] = True
            hitl_data.setdefault("ticket_id", resp.headers.get("X-Fiber-HITL-Ticket", ""))
            return hitl_data

        resp.raise_for_status()
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {"data": resp.text, "status_code": resp.status_code}

    # Local Binary / Shell Script (`exec://<command_or_path>`)
    async def _invoke_subprocess(
        self,
        parsed: ParsedResource,
        resolved_args: Dict[str, Any],
        cursor: BranchCursor,
        ckpt_handle: str,
        extras: Dict[str, Any],
    ) -> Dict[str, Any]:
        argv = shlex.split(parsed.target)
        if not argv:
            raise ValueError(f"[PolyglotBridge] Empty command in '{parsed.raw_uri}'")

        cmd_head = argv[0]
        if "/" in cmd_head and not cmd_head.startswith("/") and (self.allow_exec_subprocess or self.loader.local_repo_dir):
            try:
                return await asyncio.to_thread(self.loader.run_exec, parsed.target, resolved_args)
            except Exception:
                if not self.allow_exec_subprocess:
                    raise

        if not self.allow_exec_subprocess:
            raise PermissionError(
                f"[PolyglotBridge:{cursor.stream_id}] Direct 'exec://' subprocess execution is disabled. "
                f"Mount via 'fiber connect' as 'mcp://<target>' or enable allow_exec_subprocess=True."
            )

        stdin_envelope = json.dumps(
            {
                "checkpoint_handle": ckpt_handle,
                "stream_id": cursor.stream_id,
                "node": cursor.curr_node,
                "tick": cursor.tick,
                "arguments": resolved_args,
                "meta": cursor.meta_ctx,
            }
        ).encode("utf-8")

        timeout = float(extras.get("timeout", self.exec_timeout))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(input=stdin_envelope), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise TimeoutError(f"[PolyglotBridge:{cursor.stream_id}] Subprocess '{argv[0]}' timed out after {timeout}s")

        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode == 202:
            out = self._try_parse_json(stdout_text)
            out["_hitl_yield"] = True
            return out

        if proc.returncode != 0:
            raise RuntimeError(
                f"[PolyglotBridge:{cursor.stream_id}] Subprocess '{argv[0]}' failed (exit={proc.returncode}): "
                f"{stderr_text or stdout_text}"
            )

        return self._try_parse_json(stdout_text)

    # Helpers: Flexible Callable Invocation & Shared Contract Projection
    @staticmethod
    async def _call_flexible_signature(
        fn: LocalCallable,
        resolved_args: Dict[str, Any],
        meta_ctx: Dict[str, Any],
        ckpt_handle: str,
    ) -> Dict[str, Any]:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        param_names = {
            p.name
            for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        pos_params = [
            p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        has_pos_only = any(p.kind == inspect.Parameter.POSITIONAL_ONLY for p in params)

        if resolved_args and not has_pos_only and (
            set(resolved_args.keys()).issubset(param_names) or has_var_kw
        ):
            res = fn(**resolved_args)
        elif len(pos_params) == 1 and len(resolved_args) == 1 and next(iter(resolved_args.keys())) not in ("args", "params", "data"):
            first_param_name = pos_params[0].name
            if first_param_name not in ("args", "resolved_args", "params", "payload", "data"):
                res = fn(next(iter(resolved_args.values())))
            else:
                res = fn(resolved_args)
        elif len(pos_params) >= 3:
            res = fn(resolved_args, meta_ctx, ckpt_handle)
        elif len(pos_params) == 2:
            res = fn(resolved_args, meta_ctx)
        else:
            res = fn(resolved_args)

        if inspect.isawaitable(res):
            res = await res

        return res if isinstance(res, dict) else {"data": res}

    @staticmethod
    def _finalize_output(
        cursor: BranchCursor,
        tool_result: Any,
        projection: Optional[Dict[str, Any]],
        extras: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any], int]:
        payload_dict = tool_result if isinstance(tool_result, dict) else {"data": tool_result}

        if payload_dict.get("_hitl_yield") is True:
            ticket_id = str(payload_dict.get("_hitl_ticket") or payload_dict.get("ticket_id") or "")
            log.info(f"[PolyglotBridge:{cursor.stream_id}] Pre-Flight HITL Yield triggered at '{cursor.curr_node}'.")
            return (
                {"_hitl_yield": True, "_hitl_ticket": ticket_id},
                payload_dict,
                0,
            )

        meta_updates, offload_payload = apply_projection(
            raw_output=payload_dict,
            projection=projection,
            base_meta={"intent_type": "tool_completed", "_hitl_yield": False},
        )
        default_cost = int(extras.get("step_cost", payload_dict.get("step_cost", 5)))
        return meta_updates, offload_payload, max(1, default_cost)

    @staticmethod
    def _try_parse_json(raw_text: str) -> Dict[str, Any]:
        if not raw_text:
            return {"data": ""}
        try:
            parsed = json.loads(raw_text)
            return parsed if isinstance(parsed, dict) else {"data": parsed}
        except Exception:
            return {"data": raw_text}