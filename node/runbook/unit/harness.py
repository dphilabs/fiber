# fiber.node.runbook.unit.harness
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import importlib
import inspect
import json
from pathlib import Path
from typing import Any

from xphi.arch.contract.registry.flow import FlowRegistry
from xphi.arch.contract.reflex.auth import allowed_account_slots, missing_credentials
from xphi.arch.contract.reflex.flow import Shape, UnitContract
from xphi.arch.contract.reflex.multiflow import split_unit_ref


class CallerVerificationError(AssertionError):
    """Raised when an isolated unit call, auth binding, or runtime output violates contract."""


@dataclass(slots=True)
class CallReport:
    unit: str
    scheme: str
    mode: str
    account: str = "default"
    auth_header: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "scheme": self.scheme,
            "mode": self.mode,
            "account": self.account,
            "auth_header": self.auth_header,
            "output_keys": sorted(self.outputs),
            "warnings": self.warnings,
        }


def infer_runtime_shape(value: Any) -> Shape:
    """Map a concrete Python value directly to its contract Shape without parser detours."""
    if value is None:
        return Shape()
    if isinstance(value, bool):
        return Shape("bool")
    if isinstance(value, (int, float)):
        return Shape("num")
    if isinstance(value, str):
        return Shape("text")
    if isinstance(value, Mapping):
        return Shape("obj")
    if isinstance(value, (list, tuple)):
        if not value:
            return Shape().collected()
        elem = infer_runtime_shape(value[0])
        for item in value[1:]:
            if infer_runtime_shape(item) != elem:
                return Shape().collected()
        return elem.collected()
    return Shape()


def _mask_header(header_val: str) -> str:
    if not header_val:
        return ""
    scheme, _, token = header_val.partition(" ")
    if not token:
        return "***"
    if len(token) <= 8:
        return f"{scheme} ***"
    return f"{scheme} {token[:4]}...{token[-4:]}"


def _unit_auth_specs(unit: UnitContract) -> set[str]:
    raw = unit.auth
    if not raw:
        return {f"mcp:{unit.target.partition('/')[0]}"} if unit.scheme == "mcp" and unit.target else set()
    if isinstance(raw, str):
        return {raw}
    return {str(x) for x in raw if str(x).strip()}


def probe_unit_auth(
    unit: UnitContract,
    *,
    account: str = "default",
    available: set[str] | None = None,
    live_env: bool = False,
) -> str:
    """Validate credential availability and optionally resolve masked Authorization header."""
    if unit.scheme == "llm" and unit.auth:
        raise CallerVerificationError(f"llm unit '{unit.name}' must not declare auth.")

    specs = _unit_auth_specs(unit)
    if not specs:
        return ""

    if available is not None:
        if missing := missing_credentials(specs, available):
            raise CallerVerificationError(f"{unit.name}: missing credentials {sorted(missing)}.")

    spec = sorted(specs)[0]
    if not live_env:
        if spec.startswith("mcp:"):
            return f"MCP-Target {spec.partition(':')[2]}"
        return f"Simulated {spec}@{account}"

    from reflexor.units.adapters.auth import google as google_auth
    from reflexor.units.adapters.auth import token as token_auth

    provider, _, scope = spec.partition(":")
    if provider == "mcp":
        return f"MCP-Target {scope}"
    if provider == "google":
        tok = google_auth.resolve_token(scope or "sheets.readonly", account=account)
        return _mask_header(f"Bearer {tok}")

    tok = token_auth.resolve_token(provider, account=account)
    fmt = getattr(token_auth, "format_authorization", None)
    hdr = fmt(tok) if callable(fmt) else f"Bearer {tok}"
    return _mask_header(hdr)


def unwrap_mcp_result(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize JSON-RPC / Remote MCP `tools/call` responses into a flat out(...) dict."""
    if envelope.get("error"):
        raise CallerVerificationError(f"MCP JSON-RPC error: {envelope['error']}")

    res = envelope.get("result", envelope)
    if not isinstance(res, Mapping):
        raise CallerVerificationError(f"MCP result must be an object, got {type(res).__name__}.")

    if res.get("isError") is True:
        blocks = res.get("content") or []
        detail = " ".join(str(b.get("text", "")) for b in blocks if isinstance(b, Mapping)).strip()
        raise CallerVerificationError(f"Remote MCP tool returned isError=True: {detail or res}")

    if isinstance(res.get("structuredContent"), Mapping):
        return dict(res["structuredContent"])

    if isinstance(res.get("content"), list):
        texts = [
            str(b["text"])
            for b in res["content"]
            if isinstance(b, Mapping) and b.get("type") == "text" and "text" in b
        ]
        joined = "\n".join(texts).strip()
        if joined.startswith("{") and joined.endswith("}"):
            try:
                parsed = json.loads(joined)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
        return {"content": joined}

    return dict(res)


def _prepare_unit_inputs(
    unit: UnitContract,
    inputs: Mapping[str, Any],
    cfg: Mapping[str, Any],
    rep: CallReport,
) -> tuple[dict[str, Any], dict[str, Any]]:
    in_defaults = dict(unit.in_defaults) if isinstance(unit.in_defaults, Mapping) else {}
    cfg_defaults = dict(unit.cfg_defaults) if isinstance(unit.cfg_defaults, Mapping) else {}

    in_allowed = set(unit.in_reqs) | set(unit.in_defaults) | set(unit.in_optional)
    cfg_allowed = set(unit.cfg_reqs) | set(unit.cfg_defaults) | allowed_account_slots(unit)

    for k in sorted(set(inputs) - in_allowed):
        rep.errors.append(f"{unit.name}: unexpected input '{k}' (allowed: {sorted(in_allowed)}).")
    for k in sorted(set(cfg) - cfg_allowed):
        rep.errors.append(f"{unit.name}: unexpected cfg '{k}' (allowed: {sorted(cfg_allowed)}).")

    merged_in = {**in_defaults, **inputs}
    merged_cfg = {**cfg_defaults, **cfg}

    for k in unit.in_reqs:
        if k not in merged_in or merged_in[k] is None:
            rep.errors.append(f"{unit.name}: missing required input '{k}'.")
    for k in unit.cfg_reqs:
        if k not in merged_cfg or merged_cfg[k] is None:
            rep.errors.append(f"{unit.name}: missing required cfg '{k}'.")

    for k, val in merged_in.items():
        if val is None and k in unit.in_optional:
            continue
        expected = unit.in_shapes.get(k, Shape())
        actual = infer_runtime_shape(val)
        verdict = actual.compat(expected)
        msg = f"{unit.name}: in '{k}' runtime {actual} -> expected {expected}"
        if verdict == "error":
            rep.errors.append(msg)
        elif verdict == "warn":
            rep.warnings.append(msg)

    return merged_in, merged_cfg


def _verify_unit_outputs(unit: UnitContract, raw_out: Any, rep: CallReport) -> dict[str, Any]:
    if not isinstance(raw_out, Mapping):
        rep.errors.append(f"{unit.name}: unit must return a mapping, got {type(raw_out).__name__}.")
        return {}

    out_dict = dict(raw_out)
    optional = set(unit.out_optional)

    for k in unit.out_keys:
        if k not in out_dict or out_dict[k] is None:
            if k not in optional:
                rep.errors.append(f"{unit.name}: missing required output '{k}'.")
            continue
        expected = unit.out_shapes.get(k, Shape())
        actual = infer_runtime_shape(out_dict[k])
        verdict = actual.compat(expected)
        msg = f"{unit.name}: out '{k}' runtime {actual} -> expected {expected}"
        if verdict == "error":
            rep.errors.append(msg)
        elif verdict == "warn":
            rep.warnings.append(msg)

    return out_dict


def _invoke_py_target(target: str, inputs: dict[str, Any], cfg: dict[str, Any]) -> Any:
    mod_part, sep, fn_name = target.partition(":")
    combined = {**cfg, **inputs}

    if mod_part.endswith(".py") or "/" in mod_part:
        from fiber.node.runbook.unit.loader import run_unit_script

        path = Path(mod_part)
        if not path.is_file():
            raise CallerVerificationError(f"Script target not found: '{mod_part}'.")
        return run_unit_script(path, entry=fn_name or "run", payload=combined)

    if not sep or not mod_part or not fn_name:
        raise CallerVerificationError(f"Invalid py target '{target}', expected 'module.path:callable'.")

    module = importlib.import_module(mod_part)
    fn = getattr(module, fn_name)
    params = list(inspect.signature(fn).parameters.values())
    param_names = {p.name for p in params}
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)

    if {"inputs", "cfg"} <= param_names:
        result = fn(inputs=inputs, cfg=cfg)
    elif len(params) == 1 and params[0].name in {"payload", "ctx", "context", "state", "data"} and params[0].name not in combined:
        result = fn(combined)
    else:
        kwargs = combined if accepts_var_kw else {k: v for k, v in combined.items() if k in param_names}
        result = fn(**kwargs)

    return asyncio.run(result) if inspect.iscoroutine(result) else result


def _resolve_fixture(unit_name: str, fixtures: Mapping[str, Any] | None) -> tuple[bool, Any]:
    if not fixtures:
        return False, None
    if unit_name in fixtures:
        return True, fixtures[unit_name]
    base_unit, entry = split_unit_ref(unit_name)
    if entry and base_unit in fixtures:
        sub = fixtures[base_unit]
        if isinstance(sub, Mapping) and entry in sub:
            return True, sub[entry]
    return False, None


def call_unit(
    reg: FlowRegistry,
    unit_name: str,
    *,
    inputs: Mapping[str, Any] | None = None,
    cfg: Mapping[str, Any] | None = None,
    fixtures: Mapping[str, Any] | None = None,
    mcp_invoker: Callable[[str, str, dict[str, Any], dict[str, Any]], Mapping[str, Any]] | None = None,
    available_auth: set[str] | None = None,
    live_auth: bool = False,
) -> CallReport:
    """Execute or replay a single UnitContract (`unit` or `unit:name`) in isolation."""
    c = reg.get(unit_name)
    if not isinstance(c, UnitContract):
        raise CallerVerificationError(f"'{unit_name}' is a FlowContract, expected UnitContract.")

    cfg_map = dict(cfg or {})
    account = str(cfg_map.get("account", "default"))
    has_fx, fx_val = _resolve_fixture(unit_name, fixtures)
    rep = CallReport(unit=unit_name, scheme=c.scheme, mode="fixture" if has_fx else c.scheme, account=account)

    merged_in, merged_cfg = _prepare_unit_inputs(c, inputs or {}, cfg_map, rep)
    try:
        rep.auth_header = probe_unit_auth(c, account=account, available=available_auth, live_env=live_auth)
    except CallerVerificationError as exc:
        rep.errors.append(str(exc))

    if rep.errors:
        return rep

    try:
        if has_fx:
            raw_out = unwrap_mcp_result(fx_val) if c.scheme == "mcp" and isinstance(fx_val, Mapping) else fx_val
        elif c.scheme == "py":
            raw_out = _invoke_py_target(c.target, merged_in, merged_cfg)
        elif c.scheme == "mcp":
            if mcp_invoker is None:
                raise CallerVerificationError(f"{unit_name}: mcp_invoker or fixture required for mcp unit.")
            server_id, _, tool_name = c.target.partition("/")
            envelope = mcp_invoker(server_id, tool_name or unit_name, merged_in, {"cfg": merged_cfg, "account": account})
            raw_out = unwrap_mcp_result(envelope)
        else:
            raise CallerVerificationError(f"{unit_name}: unsupported live dispatch for scheme '{c.scheme}' without fixture.")
    except CallerVerificationError as exc:
        rep.errors.append(str(exc))
        return rep

    rep.outputs = _verify_unit_outputs(c, raw_out, rep)
    return rep