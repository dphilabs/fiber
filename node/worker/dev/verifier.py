# fiber.node.worker.dev.verifier
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, Optional

from fiber.node.bound.verify.server import VerificationError, VerificationServer
from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol

class VerifierWorker(AsyncWorkerProtocol):
    """Daemon worker exposing a resident VerificationServer over MCP."""

    def __init__(self, workspace_root: str = ".", rules_path: Optional[str] = None) -> None:
        effective_rules = (rules_path or os.environ.get("FIBER_VERIFY_RULES_PATH") or "").strip() or None
        self.server = VerificationServer(workspace_root=workspace_root, rules_path=effective_rules)
        super().__init__(agent_name="worker.verifier")

    async def close(self) -> None:
        await self.server.aclose()

    @tool
    async def list_rules(self) -> Dict[str, Any]:
        """List loaded payload rules, HTTP probes, and local binary probes."""
        return self.server.list_rules()

    @tool
    async def verify_payload(
        self,
        rule_id: str,
        payload_json: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Validate a JSON payload against the schema and semantic rules of rule_id.

        Args:
            rule_id: Rule ID defined under payload_rules in rules.json.
            payload_json: Target JSON string to validate.
            context: Optional semantic verification context (e.g., {"query": "...", "scope_prefix": "auth"}).
        """
        clean_rule = rule_id.strip()
        if not clean_rule:
            raise ToolError("'rule_id' must not be empty.")

        try:
            data = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError) as e:
            raise ToolError(f"Invalid JSON input: {e}") from None

        try:
            return await self.server.verify_payload(rule_id=clean_rule, payload=data, context=context)
        except VerificationError as e:
            raise ToolError(str(e)) from None

    @tool
    async def verify_http(
        self,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
        body_override: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Probe a local DPoP gateway or HTTP endpoint and validate its response status and schema.

        Args:
            probe_id: Probe ID defined under http_probes in rules.json (e.g., 'fiber.gateway.tools_list').
            params: URL path template parameters (e.g., {"worker": "worker.duck"}).
            body_override: Optional JSON object overriding the default request body.
            context: Optional semantic verification context.
        """
        clean_probe = probe_id.strip()
        if not clean_probe:
            raise ToolError("'probe_id' must not be empty.")

        try:
            return await self.server.verify_http(
                probe_id=clean_probe,
                params=params,
                body_override=body_override,
                context=context,
            )
        except VerificationError as e:
            raise ToolError(str(e)) from None

    @tool
    async def verify_binary(
        self,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
        rel_cwd: str = ".",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run a pre-registered CLI binary profile (e.g., git, rg) and validate its exit code and output.

        Args:
            probe_id: Probe ID defined under binary_probes in rules.json (e.g., 'git.worktree_clean').
            params: Argument template parameters (e.g., {"target_path": "src/"}).
            rel_cwd: Working directory relative to workspace root.
            context: Optional semantic verification context.
        """
        clean_probe = probe_id.strip()
        if not clean_probe:
            raise ToolError("'probe_id' must not be empty.")

        try:
            return await self.server.verify_binary(
                probe_id=clean_probe,
                params=params,
                rel_cwd=rel_cwd,
                context=context,
            )
        except VerificationError as e:
            raise ToolError(str(e)) from None


async def _serve(workspace_root: str = ".", rules_path: Optional[str] = None) -> None:
    worker = VerifierWorker(workspace_root=workspace_root, rules_path=rules_path)
    try:
        await worker.serve_forever_async()
    finally:
        await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fiber Resident Verification MCP Worker")
    parser.add_argument("--root", default=".", help="Workspace root directory for binary probes")
    parser.add_argument("--rules", default="", help="Optional path to custom rules.json")
    args = parser.parse_args()

    try:
        asyncio.run(_serve(workspace_root=args.root, rules_path=args.rules or None))
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()