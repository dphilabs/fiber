# fiber.llm.vcr.gist.setup
from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fiber.llm.pipeline.bootstrap import PipelineBootstrap
from fiber.llm.vcr.injector import VCRInjector
from fiber.llm.vcr.manager import VCRIdentityRule, VCRPlaybackConfig
from xphi.arch.contract.event.next import next_trace_id

COMPATS_REPO_URL = "https://github.com/dphilabs/fiber-compats.git"
DEFAULT_SUITE_REL = "suites/gist"
DEFAULT_INVOKER = "gist"
DEFAULT_SCENARIO = "tester_guardrail_try"


def _find_local_compats_root() -> Path | None:
    if env_dir := os.environ.get("FIBER_COMPATS_DIR"):
        p = Path(env_dir)
        if p.exists():
            return p

    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        for candidate in (base / "fiber-compats", base / "dphilabs" / "fiber-compats"):
            if candidate.is_dir():
                return candidate
    return None


def ensure_compats_fixture_dir(suite_rel: str = DEFAULT_SUITE_REL, mode: str = "replay") -> Path:
    local_root = _find_local_compats_root()
    if local_root is not None:
        target_dir = local_root / suite_rel
        if mode == "record":
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir
        if target_dir.exists():
            return target_dir

    tmp_root = Path(tempfile.gettempdir()) / "fiber-compats"
    if not (tmp_root / ".git").exists():
        subprocess.run(
            ["git", "clone", "--depth", "1", "-q", COMPATS_REPO_URL, str(tmp_root)],
            check=True,
        )
    target_dir = tmp_root / suite_rel
    if mode == "record":
        target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


@contextmanager
def vcr_gist_session(
    scenario: str = DEFAULT_SCENARIO,
    invoker: str = DEFAULT_INVOKER,
    suite_rel: str = DEFAULT_SUITE_REL,
    mode: str | None = None,
    speed: str = "max",
) -> Iterator[None]:
    """
    블록 내 모든 acompletion() 호출에 고정된 invoker 및 scenario(다중 호출 시 _2, _3...)와
    결정론적 trace_id를 자동 주입하여 unknown_invoker_auto 생성을 원천 차단합니다.
    """
    resolved_mode = mode or os.environ.get("VCR_MODE") or os.environ.get("VCR") or "replay"
    fixture_dir = ensure_compats_fixture_dir(suite_rel=suite_rel, mode=resolved_mode)
    vcr_cfg = VCRPlaybackConfig(mode=resolved_mode, speed=speed)
    VCRInjector.apply(config=vcr_cfg, fixture_dir=str(fixture_dir))

    orig_execute = PipelineBootstrap.execute_completion
    call_idx = 0

    async def _pinned_execute(model: str, messages: list[dict], **kwargs):
        nonlocal call_idx
        call_idx += 1
        meta = dict(kwargs.get("metadata") or {})
        pinned_scenario = meta.get("vcr_scenario") or (
            scenario if call_idx == 1 else f"{scenario}_{call_idx}"
        )
        pinned_invoker = meta.get("vcr_invoker") or invoker

        meta["vcr_scenario"] = pinned_scenario
        meta["vcr_invoker"] = pinned_invoker
        kwargs["metadata"] = meta

        if "trace_id" not in kwargs and resolved_mode in ("record", "replay"):
            seed = VCRIdentityRule.generate_seed(
                scenario_name=pinned_scenario,
                messages=messages,
                invoker=pinned_invoker,
            )
            kwargs["trace_id"] = next_trace_id(seed)

        return await orig_execute(model, messages, **kwargs)

    PipelineBootstrap.execute_completion = staticmethod(_pinned_execute)
    try:
        yield
    finally:
        PipelineBootstrap.execute_completion = orig_execute
        VCRInjector.remove()