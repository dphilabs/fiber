# fiber.dev.base.workflow.audit.context
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from fiber.gateway.bridge.channel.oob import AttestedResumeResult
from xphi.arch.bound.adapter.gateway import DPoPClientGenerator
import xphi.node.rpc.registry as rpc_registry
from xphi.node.rpc.handler.validator import ValidatorService
from xphi.node.space.bind.resolver import resolve_path


class MCPGatewayTestClient:
    """
    Unified DPoP-authenticated Gateway Test Client.
    Supports both single default `target_id` (worker.audit) and per-call `target_id` switching (hitl.slack).
    """

    def __init__(
        self,
        base_url: str,
        target_id: str = "",
        spiffe_id: str = "spiffe://self/fiber",
        default_timeout: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.target_id = target_id
        self.spiffe_id = spiffe_id
        self.default_timeout = default_timeout
        self.dpop = DPoPClientGenerator(key_size=2048)
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def invoke_url(self) -> str:
        return self._resolve_url(self.target_id)

    def _resolve_url(self, target_id: Optional[str] = None) -> str:
        tid = (target_id or self.target_id).strip()
        if not tid:
            raise ValueError("target_id must be specified either at initialization or per invoke() call.")
        return f"{self.base_url}/v1/mcp-gateway/{tid}/invoke"

    def _build_headers(self, url: str, idempotency_key: str, receipt: str = "valid_receipt") -> Dict[str, str]:
        nonce = uuid.uuid4().hex
        proof = self.dpop.generate_proof(url=url, method="POST", nonce=nonce)
        return {
            "x-idempotency-key": idempotency_key,
            "x-nonce": nonce,
            "X-Fiber-Fuel-Receipt": receipt,
            "x-spiffe-id": self.spiffe_id,
            "DPoP": proof,
        }

    async def invoke(
        self,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
        timeout: Optional[float] = None,
        *,
        target_id: Optional[str] = None,
        receipt: str = "valid_receipt",
    ) -> httpx.Response:
        url = self._resolve_url(target_id)
        idem_key = idempotency_key or uuid.uuid4().hex
        req_timeout = timeout if timeout is not None else self.default_timeout
        headers = self._build_headers(url=url, idempotency_key=idem_key, receipt=receipt)

        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.default_timeout)

        return await self._client.post(url, json=payload, headers=headers, timeout=req_timeout)

    async def post_gateway(
        self,
        target_id: str,
        payload: Dict[str, Any],
        idem_key: Optional[str] = None,
        timeout: float = 20.0,
    ) -> httpx.Response:
        """Multi-target convenience alias used by HITL E2E suites."""
        return await self.invoke(payload, idempotency_key=idem_key, timeout=timeout, target_id=target_id)

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def extract_handle_id(response: httpx.Response, fallback: Optional[str] = None) -> str:
        location = response.headers.get("Location", "")
        handle_id = location.split("/")[-1] if "/" in location else (fallback or "")
        if not handle_id:
            raise RuntimeError("Failed to extract handle_id from HTTP 202 Location header.")
        return handle_id

    @staticmethod
    def build_migration_payload(
        req_id: int, service_name: str, target_env: str = "production", sql_script: str = "DROP TABLE"
    ) -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": "execute_db_migration",
                "arguments": {
                    "service_name": service_name,
                    "target_env": target_env,
                    "sql_script": sql_script,
                },
            },
        }

    @staticmethod
    def attach_totp_resume(
        base_payload: Dict[str, Any],
        prompt_id: str,
        totp_code: str,
        *,
        attested_user_id: Optional[str] = None,
        override_args: Optional[Dict[str, Any]] = None,
        lease_ttl_sec: Optional[int] = None,
        quorum_sigs: Optional[List[str]] = None,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Attaches an in-band `_meta.inputResponses` envelope backed by `AttestedResumeResult`.
        Keeps wire format 100% compatible with legacy callers when optional fields are omitted.
        """
        resume_model = AttestedResumeResult(
            value=totp_code,
            attested_user_id=attested_user_id or "",
            override_args=override_args,
            lease_ttl_sec=lease_ttl_sec,
            quorum_sigs=quorum_sigs,
            comment=comment,
        )
        result_dict = resume_model.to_dict()
        if not attested_user_id:
            result_dict.pop("attested_user_id", None)

        resumed = dict(base_payload)
        params_copy = dict(resumed.get("params", {}))
        params_copy["_meta"] = {
            "inputResponses": {
                "jsonrpc": "2.0",
                "id": prompt_id,
                "result": result_dict,
            }
        }
        resumed["params"] = params_copy
        return resumed


class AuditContext:
    def __init__(
        self,
        spiffe_id: str = "spiffe://self/fiber",
        tenant_id: str = "accounting_dept",
        totp_secret: str = "JBSWY3DPEHPK3PXP",
        passphrase: Optional[str] = None,
    ):
        self.run_id = uuid.uuid4().hex[:8]
        self.run_suffix = self.run_id[:6]
        base_id = spiffe_id.rstrip("/")
        base_tenant = tenant_id.strip()
        self.spiffe_id = f"{base_id}_e2e_{self.run_id}"
        self.tenant_id = f"{base_tenant}_e2e_{self.run_id}"
        self.totp_secret = totp_secret
        self._explicit_passphrase = passphrase

        self.db_path = os.environ.get(
            "DPHI_AUDIT_DB_PATH",
            os.path.join(str(resolve_path("sign")), "deploy_audit.sqlite"),
        )
        self.validator_service: Optional[ValidatorService] = None
        self._original_rpc_builder = None
        self._managed_clients: List[MCPGatewayTestClient] = []

    @property
    def temp_db_path(self) -> str:
        return f"{self.db_path} (Scoped ID: {self.spiffe_id}, Tenant: {self.tenant_id})"

    def scoped_target(self, base_target_id: str) -> str:
        """Returns a process-isolated target_id to prevent Redis Pub/Sub collision with live daemons."""
        return f"{base_target_id.rstrip('-')}-e2e-{self.run_suffix}"

    def create_gateway_client(
        self, base_url: str, target_id: str = "", default_timeout: float = 20.0
    ) -> MCPGatewayTestClient:
        """Creates and tracks a DPoP gateway client bound to this AuditContext's SPIFFE ID."""
        client = MCPGatewayTestClient(
            base_url=base_url,
            target_id=target_id,
            spiffe_id=self.spiffe_id,
            default_timeout=default_timeout,
        )
        self._managed_clients.append(client)
        return client

    def provision(self) -> ValidatorService:
        """Initializes ValidatorService and injects an isolated ephemeral admin record into SQLite."""
        self.validator_service = ValidatorService()
        active_passphrase = self._explicit_passphrase or self.validator_service.master_passphrase

        salt = os.urandom(16)
        nonce = os.urandom(12)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
        aesgcm = AESGCM(kdf.derive(active_passphrase.encode("utf-8")))
        ciphertext = aesgcm.encrypt(nonce, self.totp_secret.encode("utf-8"), None)

        self.validator_service.conn.execute(
            "INSERT OR REPLACE INTO admin_users VALUES (?, ?, ?, ?)",
            (self.spiffe_id, salt.hex(), nonce.hex(), ciphertext.hex()),
        )
        self.validator_service.conn.commit()

        self._original_rpc_builder = rpc_registry.build_internal_rpc_registry
        validator_ref = self.validator_service

        def _injected_builder(*args, **kwargs):
            kwargs["validator_service"] = validator_ref
            return self._original_rpc_builder(*args, **kwargs)

        rpc_registry.build_internal_rpc_registry = _injected_builder
        return self.validator_service

    def generate_totp(self) -> str:
        """Generates a valid 6-digit RFC 6238 TOTP code for the current 30s window."""
        key = base64.b32decode(self.totp_secret, True)
        msg = struct.pack(">Q", int(time.time()) // 30)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        o = h[19] & 15
        code = (struct.unpack(">I", h[o : o + 4])[0] & 0x7FFFFFFF) % 1000000
        return f"{code:06d}"

    async def aclose_clients(self) -> None:
        for client in self._managed_clients:
            await client.aclose()
        self._managed_clients.clear()

    def teardown(self) -> None:
        """Restores RPC registry hook and purges only this run's scoped admin_users record."""
        if self._original_rpc_builder:
            rpc_registry.build_internal_rpc_registry = self._original_rpc_builder
            self._original_rpc_builder = None

        if self.validator_service and hasattr(self.validator_service, "conn"):
            try:
                self.validator_service.conn.execute(
                    "DELETE FROM admin_users WHERE user_id = ?", (self.spiffe_id,)
                )
                self.validator_service.conn.commit()
                self.validator_service.conn.close()
            except Exception:
                pass