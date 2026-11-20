# fiber.node.mcp.quarantine
from __future__ import annotations

import base64
import os
import re
from typing import Any, Dict, List, Optional

import orjson
from fiber.gateway.bridge.emitter import get_emitter

log = get_emitter("mcp.quarantine")

_SSE_CONTROL_PREFIXES = ("event:", ":", "id:", "retry:")
_IGNORED_TARGET_TOKENS = frozenset({"NET", "REMOTE", "LOCAL", "MCP", "SERVER", "WORKER"})
_EXPLICIT_AUTH_SCHEMES = ("bearer ", "basic ", "token ", "apikey ", "digest ", "aws4-hmac-")

class IsolationAdapter:
    """
    Connector-side I/O quarantine boundary.
    - Ingress: Injects normalized HTTP headers & auth tokens strictly for remote network targets.
    - Egress: Strips SSE framing and enforces strict JSON object IPC contracts.
    """
    def __init__(
        self,
        target_id: str = "",
        *,
        is_remote: bool = True,
        env: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.target_id = target_id
        self.is_remote = is_remote
        self._local_env: Dict[str, str] = {
            str(k): str(v) for k, v in (env or {}).items() if v is not None
        }
        self._static_headers: Dict[str, str] = {
            str(k).strip(): str(v)
            for k, v in (headers or {}).items()
            if str(k).strip() and v is not None
        }
        self._env_key_candidates = self._build_env_candidates(target_id)

    @staticmethod
    def _build_env_candidates(target_id: str) -> List[str]:
        if not target_id:
            return []
        norm = re.sub(r"[^A-Z0-9]+", "_", target_id.upper()).strip("_")
        tokens = [
            t for t in norm.split("_")
            if t and t not in _IGNORED_TARGET_TOKENS and not t.isdigit()
        ]

        candidates: List[str] = []
        if tokens:
            head = tokens[0]
            # Aligned with reflexunits.adapters.auth.token.PROVIDER_ENV_KEYS + legacy *_API_KEY
            candidates.extend([
                f"FIBER_{head}_TOKEN",
                f"{head}_API_KEY",
                f"{head}_API_TOKEN",
                f"{head}_TOKEN",
            ])
        candidates.extend([
            f"MCP_{norm}_API_KEY",
            f"MCP_{norm}_TOKEN",
        ])
        # Deduplicate while preserving priority order
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _format_auth_header(raw_token: str, scheme_hint: Optional[str] = None) -> str:
        val = raw_token.strip()
        if not val:
            return ""

        # 1. Already carries an explicit HTTP Authorization scheme
        if val.lower().startswith(_EXPLICIT_AUTH_SCHEMES):
            return val

        # 2. Explicit scheme override (e.g. FIBER_AUTH_SCHEME=Basic | ApiKey | raw)
        if scheme_hint:
            hint = scheme_hint.strip()
            if hint.lower() in ("raw", "none", "plain"):
                return val
            if hint.lower() == "basic" and ":" in val:
                encoded = base64.b64encode(val.encode("utf-8")).decode("ascii")
                return f"Basic {encoded}"
            return f"{hint} {val}"

        # 3. "user_or_email:api_token" pattern (e.g. Atlassian Cloud Basic auth)
        if ":" in val and " " not in val and not val.startswith(("http://", "https://")):
            encoded = base64.b64encode(val.encode("utf-8")).decode("ascii")
            return f"Basic {encoded}"

        # 4. Standard opaque / JWT token fallback
        return f"Bearer {val}"

    def _lookup_env(self, key: str) -> str:
        if key in self._local_env:
            return self._local_env[key].strip()
        return os.environ.get(key, "").strip()

    @staticmethod
    def _has_auth_header(headers: Dict[str, str]) -> bool:
        return any(k.lower() == "authorization" for k in headers)

    def _resolve_auth_value(self, meta_token: Optional[str] = None) -> str:
        scheme_hint = self._lookup_env("FIBER_AUTH_SCHEME") or None

        if meta_token and meta_token.strip():
            return self._format_auth_header(meta_token, scheme_hint=scheme_hint)

        explicit_env_name = self._lookup_env("FIBER_AUTH_ENV")
        search_keys = (
            [explicit_env_name, *self._env_key_candidates]
            if explicit_env_name
            else self._env_key_candidates
        )

        for env_key in search_keys:
            token = self._lookup_env(env_key)
            if token:
                return self._format_auth_header(token, scheme_hint=scheme_hint)
        return ""

    def translate_ingress(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Stdio local workers must not receive synthetic _http_headers in JSON-RPC stdin
        if not self.is_remote:
            if "_http_headers" in payload:
                cleaned = dict(payload)
                cleaned.pop("_http_headers", None)
                return cleaned
            return payload

        merged_headers: Dict[str, str] = dict(self._static_headers)
        existing_hdrs = payload.get("_http_headers")
        if isinstance(existing_hdrs, dict):
            for k, v in existing_hdrs.items():
                if v is not None:
                    merged_headers[str(k)] = str(v)

        # Extract optional per-call auth/headers passed via params._meta without mutating caller payload
        meta_token: Optional[str] = None
        params = payload.get("params")
        cleaned_params: Optional[Dict[str, Any]] = None
        if isinstance(params, dict):
            meta = params.get("_meta")
            if isinstance(meta, dict) and ("auth_token" in meta or "http_headers" in meta):
                meta_copy = dict(meta)
                raw_meta_tok = meta_copy.pop("auth_token", None)
                raw_meta_hdrs = meta_copy.pop("http_headers", None)
                if isinstance(raw_meta_tok, str) and raw_meta_tok.strip():
                    meta_token = raw_meta_tok.strip()
                if isinstance(raw_meta_hdrs, dict):
                    for k, v in raw_meta_hdrs.items():
                        if v is not None:
                            merged_headers[str(k)] = str(v)
                cleaned_params = {**params, "_meta": meta_copy}

        if not self._has_auth_header(merged_headers):
            auth_val = self._resolve_auth_value(meta_token=meta_token)
            if auth_val:
                merged_headers["Authorization"] = auth_val

        if not merged_headers and cleaned_params is None:
            return payload

        safe_payload = dict(payload)
        if cleaned_params is not None:
            safe_payload["params"] = cleaned_params
        if merged_headers:
            safe_payload["_http_headers"] = merged_headers
        return safe_payload

    def translate_egress(self, raw_output: str) -> Dict[str, Any]:
        chunk = raw_output.strip()
        if not chunk:
            raise RuntimeError("IPC Contract Violation: Agent closed stream unexpectedly.")

        if chunk.startswith(_SSE_CONTROL_PREFIXES) or chunk.startswith("data:"):
            data_lines = [
                line.strip()[5:].strip() if line.strip().startswith("data:") else line.strip()
                for line in chunk.splitlines()
                if line.strip() and not line.strip().startswith(_SSE_CONTROL_PREFIXES)
            ]
            if not data_lines:
                return {}
            chunk = "\n".join(data_lines)

        try:
            parsed = orjson.loads(chunk)
            if not isinstance(parsed, dict):
                raise ValueError("Root JSON element must be an object.")
            return parsed
        except Exception:
            log.error(f"[Quarantine] STDOUT Pollution Detected: {chunk}")
            raise RuntimeError(
                f"IPC Contract Violation: Agent stdout is not valid JSON. (Output: {chunk[:50]}...)"
            )


class AdapterRegistry:
    @staticmethod
    def get_adapter(
        target_id: str = "",
        *,
        is_remote: bool = True,
        env: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> IsolationAdapter:
        return IsolationAdapter(
            target_id=target_id,
            is_remote=is_remote,
            env=env,
            headers=headers,
        )