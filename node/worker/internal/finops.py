# fiber.node.worker.internal.finops
import json
import asyncio
from typing import Dict, Any, Optional

from fiber.node.account.finops.token.gen import FinOpsTokenGenerator, FinOpsOpType
from xphi.arch.contract.protocol.binder import tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol
from xphi.node.space.tunnel.factory import TunnelFactory

DEFAULT_TENANT_ID = "accounting_dept"
SYSTEM_ISSUER = "finops_worker"

class FinopsSystemWorker(AsyncWorkerProtocol):
    """Local FinOps control worker for JWT issuance and real-time Pub/Sub budget signaling."""

    def __init__(self):
        self.token_gen = FinOpsTokenGenerator(issuer=SYSTEM_ISSUER)
        super().__init__(agent_name="worker.finops")

    @tool
    async def issue_jwt(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        budget_usd: float = 1.0,
        op: FinOpsOpType = "SYNC_QUOTA",
        policy_ver: int = 1,
        allocation_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """Mint a standardized FinOps JWT with budget constraints, operation mode, and policy version.

        Args:
            tenant_id: Target tenant identifier.
            budget_usd: Allocated budget in USD.
            op: FinOps operation mode (PROVISION, SYNC_QUOTA, TOP_UP, REVOKE).
            policy_ver: Policy version number.
            allocation_id: Optional budget allocation identifier.
        """
        token = await self.handle_issue_jwt(
            tenant_id=tenant_id,
            budget_usd=budget_usd,
            op=op,
            policy_ver=policy_ver,
            allocation_id=allocation_id,
        )
        return {"jwt": token}

    @tool
    async def trigger_kill(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        policy_ver: Optional[int] = None,
    ) -> Dict[str, str]:
        """Publish a real-time REVOKE kill-switch signal to the FinOps control plane.

        Args:
            tenant_id: Target tenant identifier to revoke.
            policy_ver: Optional policy version to attach to the revoke payload.
        """
        await self.handle_kill_switch(tenant_id=tenant_id, policy_ver=policy_ver)
        return {"status": "KILL_SIGNAL_SENT"}

    async def handle_issue_jwt(
        self,
        tenant_id: str,
        budget_usd: float,
        op: FinOpsOpType = "SYNC_QUOTA",
        policy_ver: int = 1,
        allocation_id: Optional[str] = None
    ) -> str:
        encoded_jwt = self.token_gen.generate_token(
            tenant_id=tenant_id,
            budget_usd=budget_usd,
            op=op,
            policy_ver=policy_ver,
            allocation_id=allocation_id
        )
        self.log.info(
            f"[LocalFinOps] Minted FinOps JWT for '{tenant_id}' "
            f"(Budget: ${budget_usd}, Op: {op}, Ver: v{policy_ver})"
        )
        return encoded_jwt

    async def handle_kill_switch(self, tenant_id: str, policy_ver: Optional[int] = None):
        tunnel = await TunnelFactory.get_default()
        kill_payload: Dict[str, Any] = {
            "type": "update_budget",
            "tenant_id": tenant_id,
            "fuel_budget": 0.0,
            "finops": {
                "op": "REVOKE",
                "budget_usd": 0.0,
                "exchange_rate": 1000
            }
        }
        if policy_ver is not None:
            kill_payload["finops"]["policy_ver"] = int(policy_ver)

        await tunnel.publish("finops:control_plane", json.dumps(kill_payload))
        self.log.warning(f"[LocalFinOps] Published KILL-SWITCH (REVOKE) for tenant '{tenant_id}' via Pub/Sub")

def main():
    worker = FinopsSystemWorker()
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        worker.log.info("Local FinOps System Worker shutting down.")

if __name__ == "__main__":
    main()