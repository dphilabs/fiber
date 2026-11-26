# fiber.node.bound.verify.models
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
HttpAuthMode = Literal["none", "fiber_dpop"]

class SemanticCheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluator: str = Field(..., min_length=1, description="등록된 시맨틱 검증기 식별자")
    config: Dict[str, Any] = Field(default_factory=dict, description="임계치(threshold) 등 검증기별 전달 옵션")

class PayloadRuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    jmespath_expr: Optional[str] = None
    json_schema: Dict[str, Any]
    semantic_check: Optional[SemanticCheckSpec] = None


class HttpProbeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    method: HttpMethod = "GET"
    url: str
    auth_mode: HttpAuthMode = "none"
    spiffe_id: str = "spiffe://self/fiber"
    headers: Dict[str, str] = Field(default_factory=dict)
    body: Optional[Any] = None
    timeout_sec: float = Field(default=5.0, gt=0.0, le=60.0)
    expected_status: List[int] = Field(default_factory=lambda: [200])
    jmespath_expr: Optional[str] = None
    response_schema: Optional[Dict[str, Any]] = None
    semantic_check: Optional[SemanticCheckSpec] = None


class BinaryProbeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    executable: str
    args: List[str] = Field(default_factory=list)
    expected_exit_codes: List[int] = Field(default_factory=lambda: [0])
    parse_stdout_json: bool = False
    timeout_sec: float = Field(default=10.0, gt=0.0, le=120.0)
    jmespath_expr: Optional[str] = None
    stdout_schema: Optional[Dict[str, Any]] = None
    semantic_check: Optional[SemanticCheckSpec] = None

class VerifyRulesRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_rules: Dict[str, PayloadRuleSpec] = Field(default_factory=dict)
    http_probes: Dict[str, HttpProbeSpec] = Field(default_factory=dict)
    binary_probes: Dict[str, BinaryProbeSpec] = Field(default_factory=dict)