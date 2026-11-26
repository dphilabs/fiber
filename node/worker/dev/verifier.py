# fiber.node.worker.dev.verifier
import asyncio
import json
from typing import Any, Dict, Optional

from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol
from fiber.node.bound.verify.server import VerificationError, VerificationServer

class VerifierWorker(AsyncWorkerProtocol):
    """상주형 VerificationServer를 MCP 프로토콜로 노출하는 데몬 워커"""

    def __init__(self, workspace_root: str = ".", rules_path: Optional[str] = None):
        self.server = VerificationServer(workspace_root=workspace_root, rules_path=rules_path)
        super().__init__(agent_name="worker.verifier")

    async def close(self) -> None:
        await self.server.aclose()

    @tool
    async def list_rules(self) -> Dict[str, Any]:
        """현재 검증 서버에 로드된 페이로드 규약, HTTP 프로브, 로컬 바이너리 프로브 목록을 조회합니다."""
        return self.server.list_rules()

    @tool
    async def verify_payload(
        self,
        rule_id: str,
        payload_json: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """JSON 데이터가 지정된 rule_id의 스키마 및 선택적 시맨틱 규약을 충족하는지 검증합니다.

        Args:
            rule_id: rules.json의 payload_rules에 정의된 규칙 ID
            payload_json: 검증할 대상 JSON 문자열
            context: 시맨틱 검증기에 전달할 선택적 비교 맥락 (예: {"query": "...", "scope_prefix": "auth"})
        """
        try:
            data = json.loads(payload_json)
        except json.JSONDecodeError as e:
            raise ToolError(f"Invalid JSON input: {e}")

        try:
            return await self.server.verify_payload(rule_id=rule_id, payload=data, context=context)
        except VerificationError as e:
            raise ToolError(str(e))

    @tool
    async def verify_http(
        self,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
        body_override: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """로컬 DPoP 게이트웨이 또는 외부 HTTP 엔드포인트를 호출하고 응답 상태와 스키마를 검증합니다.

        Args:
            probe_id: rules.json의 http_probes에 정의된 프로브 ID (예: fiber.gateway.tools_list)
            params: URL 경로 템플릿 치환 파라미터 (예: {"worker": "worker.duck"})
            body_override: 기본 요청 본문을 덮어쓸 JSON 객체 (선택)
            context: 시맨틱 검증기에 전달할 추가 맥락 (선택)
        """
        try:
            return await self.server.verify_http(
                probe_id=probe_id,
                params=params,
                body_override=body_override,
                context=context,
            )
        except VerificationError as e:
            raise ToolError(str(e))

    @tool
    async def verify_binary(
        self,
        probe_id: str,
        params: Optional[Dict[str, Any]] = None,
        rel_cwd: str = ".",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """사전 등록된 로컬 CLI 바이너리 프로파일(git, rg 등)을 실행하고 종료 코드와 출력을 검증합니다.

        Args:
            probe_id: rules.json의 binary_probes에 정의된 프로브 ID (예: git.worktree_clean)
            params: 인자 템플릿 치환 파라미터 (예: {"target_path": "src/"})
            rel_cwd: 워크스페이스 기준 실행 하위 디렉토리
            context: 시맨틱 검증기에 전달할 추가 맥락 (선택)
        """
        try:
            return await self.server.verify_binary(
                probe_id=probe_id,
                params=params,
                rel_cwd=rel_cwd,
                context=context,
            )
        except VerificationError as e:
            raise ToolError(str(e))


async def _serve() -> None:
    worker = VerifierWorker()
    try:
        await worker.serve_forever_async()
    finally:
        await worker.close()


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()