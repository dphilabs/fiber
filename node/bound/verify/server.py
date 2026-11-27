# fiber.node.bound.verify.server
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Awaitable, Dict, List, Optional, Protocol, Tuple, Union
import uuid

import httpx
import jmespath
from jmespath.exceptions import JMESPathError
from jsonschema import Draft202012Validator, SchemaError
from pydantic import ValidationError

from fiber.node.bound.verify.models import (
    BinaryProbeSpec,
    HttpProbeSpec,
    PayloadRuleSpec,
    SemanticCheckSpec,
    VerifyRulesRegistry,
)
from xphi.arch.bound.adapter.gateway import DPoPClientGenerator
from xphi.arch.contract.config.env import DEFAULT_GATEWAY_PORT

DEFAULT_RULES_PATH = Path(__file__).with_name("rules.json")
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

ViolationItem = Dict[str, str]
EvaluatorReturn = Union[List[ViolationItem], Awaitable[List[ViolationItem]]]


class SemanticEvaluator(Protocol):
    def __call__(
        self, target: Any, context: Dict[str, Any], config: Dict[str, Any]
    ) -> EvaluatorReturn: ...


class VerificationError(Exception):
    """규칙 미등록, 파라미터 누락, 미등록 시맨틱 검증기 호출, 실행 실패 시 발생하는 예외"""


@dataclass(frozen=True)
class _CompiledCheck:
    jmes: Optional[jmespath.parser.ParsedResult]
    validator: Optional[Draft202012Validator]
    semantic: Optional[SemanticCheckSpec] = None


def _bind_placeholders(template: str, params: Dict[str, Any], *, forbid_cli_flags: bool = False) -> str:
    merged = {"gateway_port": DEFAULT_GATEWAY_PORT, **params}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in merged or merged[key] is None:
            raise VerificationError(f"Missing required template parameter: '{key}'")
        val = str(merged[key])
        if "\x00" in val or "\r" in val or "\n" in val:
            raise VerificationError(f"Control characters are not allowed in parameter: '{key}'")
        if forbid_cli_flags:
            stripped = val.strip()
            if stripped.startswith("-"):
                raise VerificationError(f"Leading '-' (CLI option injection) is forbidden in parameter: '{key}'")
            norm_parts = stripped.replace("\\", "/").split("/")
            if ".." in norm_parts:
                raise VerificationError(f"Parent directory traversal ('..') is forbidden in parameter: '{key}'")
        return val

    return _PLACEHOLDER_RE.sub(_replace, template)


class VerificationServer:
    """표준 스키마 검증(jsonschema/jmespath), DPoP HTTP 프로브, 확장형 시맨틱 훅을 제공하는 상주형 검증 코어"""

    def __init__(
        self,
        workspace_root: str = ".",
        rules_path: Optional[str] = None,
        evaluators: Optional[Dict[str, SemanticEvaluator]] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.rules_path = Path(rules_path).expanduser().resolve() if rules_path else DEFAULT_RULES_PATH

        self._http_client: Optional[httpx.AsyncClient] = None
        self._dpop_gen: Optional[DPoPClientGenerator] = None
        self._semantic_evaluators: Dict[str, SemanticEvaluator] = dict(evaluators or {})

        self._payload_checks: Dict[str, Tuple[PayloadRuleSpec, _CompiledCheck]] = {}
        self._http_checks: Dict[str, Tuple[HttpProbeSpec, _CompiledCheck]] = {}
        self._binary_checks: Dict[str, Tuple[BinaryProbeSpec, _CompiledCheck]] = {}

        self._load_and_compile(self.rules_path)

    def register_evaluator(self, name: str, evaluator: SemanticEvaluator) -> None:
        if not name.strip():
            raise ValueError("Evaluator name must not be empty.")
        self._semantic_evaluators[name] = evaluator

    @staticmethod
    def _compile_check(
        rule_id: str,
        jmes_expr: Optional[str],
        schema: Optional[Dict[str, Any]],
        semantic: Optional[SemanticCheckSpec] = None,
    ) -> _CompiledCheck:
        compiled_jmes = None
        if jmes_expr:
            try:
                compiled_jmes = jmespath.compile(jmes_expr)
            except JMESPathError as e:
                raise VerificationError(f"Invalid JMESPath in '{rule_id}': {e}") from e

        compiled_validator = None
        if schema is not None:
            try:
                Draft202012Validator.check_schema(schema)
                compiled_validator = Draft202012Validator(schema)
            except SchemaError as e:
                raise VerificationError(f"Invalid JSON Schema in '{rule_id}': {e.message}") from e

        return _CompiledCheck(jmes=compiled_jmes, validator=compiled_validator, semantic=semantic)

    def _load_and_compile(self, path: Path) -> None:
        if not path.is_file():
            raise VerificationError(f"Rules file not found: {path}")

        try:
            registry = VerifyRulesRegistry.model_validate_json(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise VerificationError(f"Cannot read rules file '{path}': {e}") from e
        except ValidationError as e:
            raise VerificationError(f"Invalid rules.json format: {e}") from e

        self._payload_checks = {
            rid: (spec, self._compile_check(rid, spec.jmespath_expr, spec.json_schema, spec.semantic_check))
            for rid, spec in registry.payload_rules.items()
        }
        self._http_checks = {
            rid: (spec, self._compile_check(rid, spec.jmespath_expr, spec.response_schema, spec.semantic_check))
            for rid, spec in registry.http_probes.items()
        }
        self._binary_checks = {
            rid: (spec, self._compile_check(rid, spec.jmespath_expr, spec.stdout_schema, spec.semantic_check))
            for rid, spec in registry.binary_probes.items()
        }

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(follow_redirects=False)
        return self._http_client

    def _build_request_headers(self, spec: HttpProbeSpec, url: str) -> Dict[str, str]:
        headers = dict(spec.headers)
        if spec.auth_mode == "fiber_dpop":
            if self._dpop_gen is None:
                self._dpop_gen = DPoPClientGenerator(key_size=2048)
            nonce = uuid.uuid4().hex
            headers.update({
                "x-idempotency-key": uuid.uuid4().hex,
                "x-nonce": nonce,
                "x-spiffe-id": spec.spiffe_id,
                "DPoP": self._dpop_gen.generate_proof(url=url, method=spec.method, nonce=nonce),
            })
            # 하드코딩된 더미 영수증("valid_receipt") 제거: 명시적 설정이 있을 때만 헤더 부착
            env_receipt = os.environ.get("FIBER_FUEL_RECEIPT", "").strip()
            if "X-Fiber-Fuel-Receipt" not in headers and env_receipt:
                headers["X-Fiber-Fuel-Receipt"] = env_receipt
        return headers

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def _evaluate_pipeline(
        self,
        check: _CompiledCheck,
        data: Any,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[ViolationItem]:
        target = check.jmes.search(data) if check.jmes is not None else data

        if check.validator is not None:
            errors = sorted(
                check.validator.iter_errors(target),
                key=lambda e: [str(p) for p in e.absolute_path],
            )
            if errors:
                return [
                    {
                        "path": "/" + "/".join(str(p) for p in err.absolute_path),
                        "validator": str(err.validator),
                        "message": err.message,
                    }
                    for err in errors
                ]

        if check.semantic is not None:
            ev_name = check.semantic.evaluator
            evaluator = self._semantic_evaluators.get(ev_name)
            if evaluator is None:
                raise VerificationError(
                    f"Semantic evaluator '{ev_name}' is referenced by rule but not registered in server."
                )
            res = evaluator(target, context or {}, check.semantic.config)
            sem_violations = await res if inspect.isawaitable(res) else res
            return list(sem_violations)

        return []

    def list_rules(self) -> Dict[str, Any]:
        return {
            "rules_path": str(self.rules_path),
            "workspace_root": str(self.workspace_root),
            "registered_evaluators": sorted(self._semantic_evaluators.keys()),
            "payload_rules": {k: v[0].description for k, v in self._payload_checks.items()},
            "http_probes": {k: v[0].description for k, v in self._http_checks.items()},
            "binary_probes": {k: v[0].description for k, v in self._binary_checks.items()},
        }

    async def verify_payload(
        self,
        rule_id: str,
        payload: Any,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry = self._payload_checks.get(rule_id)
        if entry is None:
            raise VerificationError(f"Unknown payload rule_id: '{rule_id}'")

        _, check = entry
        violations = await self._evaluate_pipeline(check, payload, context=context)
        return {
            "rule_id": rule_id,
            "passed": len(violations) == 0,
            "violation_count": len(violations),
            "violations": violations,
        }

    async def verify_http(
        self,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
        body_override: Optional[Any] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry = self._http_checks.get(probe_id)
        if entry is None:
            raise VerificationError(f"Unknown http probe_id: '{probe_id}'")

        spec, check = entry
        bound_url = _bind_placeholders(spec.url, params or {})
        req_body = body_override if body_override is not None else spec.body
        req_headers = self._build_request_headers(spec, bound_url)

        client = self._get_http_client()
        try:
            resp = await client.request(
                method=spec.method,
                url=bound_url,
                headers=req_headers,
                json=req_body,
                timeout=spec.timeout_sec,
            )
        except httpx.HTTPError as e:
            raise VerificationError(f"HTTP request failed for '{probe_id}' ({bound_url}): {e}") from e

        violations: List[ViolationItem] = []
        if resp.status_code not in spec.expected_status:
            violations.append({
                "path": "/status_code",
                "validator": "expected_status",
                "message": f"Expected {spec.expected_status}, got {resp.status_code}",
            })

        if not violations and (check.jmes is not None or check.validator is not None or check.semantic is not None):
            try:
                parsed_body = resp.json()
            except ValueError as e:
                violations.append({
                    "path": "/body",
                    "validator": "json_decode",
                    "message": f"Response body is not valid JSON: {e}",
                })
            else:
                merged_ctx = {"params": params or {}, **(context or {})}
                violations.extend(await self._evaluate_pipeline(check, parsed_body, context=merged_ctx))

        return {
            "probe_id": probe_id,
            "url": bound_url,
            "status_code": resp.status_code,
            "passed": len(violations) == 0,
            "violation_count": len(violations),
            "violations": violations,
        }

    async def verify_binary(
        self,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
        rel_cwd: str = ".",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        entry = self._binary_checks.get(probe_id)
        if entry is None:
            raise VerificationError(f"Unknown binary probe_id: '{probe_id}'")

        spec, check = entry
        bin_path = shutil.which(spec.executable)
        if bin_path is None:
            raise VerificationError(f"Required local binary not found in PATH: '{spec.executable}'")

        clean_cwd = rel_cwd.strip() or "."
        target_cwd = (self.workspace_root / clean_cwd).resolve()
        if not target_cwd.is_relative_to(self.workspace_root) or not target_cwd.is_dir():
            raise VerificationError(f"Invalid or out-of-workspace directory: '{rel_cwd}'")

        bound_args = [
            _bind_placeholders(arg, params or {}, forbid_cli_flags=True)
            for arg in spec.args
        ]

        # stdin=DEVNULL 필수: Stdio 멀티플렉스 워커의 표준입력(JSON-RPC 스트림)을 자식 프로세스가 훔쳐 읽는 현상 차단
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            *bound_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(target_cwd),
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_sec)
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise VerificationError(f"Binary probe '{probe_id}' timed out after {spec.timeout_sec}s") from e

        exit_code = int(proc.returncode or 0)
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        violations: List[ViolationItem] = []
        if exit_code not in spec.expected_exit_codes:
            violations.append({
                "path": "/exit_code",
                "validator": "expected_exit_codes",
                "message": f"Expected {spec.expected_exit_codes}, got {exit_code}",
            })

        if not violations and (check.jmes is not None or check.validator is not None or check.semantic is not None):
            if spec.parse_stdout_json:
                try:
                    target_data: Any = json.loads(stdout_text)
                except json.JSONDecodeError as e:
                    violations.append({
                        "path": "/stdout",
                        "validator": "json_decode",
                        "message": f"Stdout is not valid JSON: {e}",
                    })
                    target_data = None
            else:
                target_data = stdout_text

            if not violations:
                merged_ctx = {"params": params or {}, **(context or {})}
                violations.extend(await self._evaluate_pipeline(check, target_data, context=merged_ctx))

        return {
            "probe_id": probe_id,
            "executable": spec.executable,
            "exit_code": exit_code,
            "passed": len(violations) == 0,
            "violation_count": len(violations),
            "violations": violations,
            "stderr_tail": stderr_text[-500:] if stderr_text else "",
        }