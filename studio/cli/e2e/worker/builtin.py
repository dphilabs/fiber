# fiber.studio.cli.e2e.worker.builtin
"""
@desc: Fixture-driven Built-in Workers (7-Target) E2E Validation Suite powered by xphi.arch.reflex.harness.suite
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from email.message import EmailMessage
import importlib
import inspect
import json
import mailbox
import math
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from fiber.studio.cli.e2e.config import E2EConfig, Phase, TestResult, get_free_port
from fiber.studio.cli.e2e.pipeline import BasePipeline, log
from fiber.studio.harness.mcp.worker import E2EWorkerManager
from xphi.arch.bound.adapter.gateway import DPoPClientGenerator
from xphi.arch.reflex.harness.suite import diff_snapshot, resolve_fixture_dir
from xphi.state.phase.reactor import PhaseReactor

FIXTURE_SUBDIR = "mcp/builtin"
SEED_FILE = "01_workspace_seed.json"
CONTRACTS_FILE = "02_worker_contracts.json"

TARGET_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("duck", "worker.duck", "fiber.node.worker.duck"),
    ("verifier", "worker.dev.verifier", "fiber.node.worker.dev.verifier"),
    ("mailbox", "worker.daily.mailbox", "fiber.node.worker.daily.mailbox"),
    ("media", "worker.daily.media", "fiber.node.worker.daily.media"),
    ("reader", "worker.reader", "fiber.node.worker.reader"),
    ("pin", "worker.memory.pin", "fiber.node.worker.memory.pin"),
    ("git", "worker.dev.git.history", "fiber.node.worker.dev.git.history"),
)


def _supports_cli_flag(module_path: str, flag: str) -> bool:
    try:
        mod = importlib.import_module(module_path)
        main_fn = getattr(mod, "main", None)
        src = inspect.getsource(main_fn if main_fn is not None else mod)
        return flag in src
    except Exception:
        return False


def _materialize_seed(seed: Mapping[str, Any], workspace_dir: Path, outside_dir: Path) -> Path:
    # 1. 일반 텍스트 파일 생성
    for rel_path, content in (seed.get("workspace_files") or {}).items():
        fpath = workspace_dir / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    # 2. Mail (.eml, .mbox, 외부 심볼릭 링크) 생성
    mail_cfg = seed.get("mail_seed") or {}
    for eml_spec in mail_cfg.get("eml", ()):
        msg = EmailMessage()
        msg["From"] = eml_spec["from"]
        msg["To"] = eml_spec["to"]
        msg["Subject"] = eml_spec["subject"]
        msg["Date"] = eml_spec["date"]
        msg.set_content(eml_spec["body"])
        if att := eml_spec.get("attachment"):
            msg.add_attachment(
                att["content"].encode("utf-8"),
                maintype="text",
                subtype="plain",
                filename=att["filename"],
            )
        out_eml = workspace_dir / eml_spec["path"]
        out_eml.parent.mkdir(parents=True, exist_ok=True)
        out_eml.write_bytes(msg.as_bytes())

    if mbox_cfg := mail_cfg.get("mbox"):
        mbox_path = workspace_dir / mbox_cfg["path"]
        mbox_path.parent.mkdir(parents=True, exist_ok=True)
        box = mailbox.mbox(mbox_path, create=True)
        try:
            for item in mbox_cfg.get("messages", ()):
                m = EmailMessage()
                m["From"] = item["from"]
                m["To"] = item["to"]
                m["Subject"] = item["subject"]
                m["Date"] = item["date"]
                m.set_content(item["body"])
                box.add(m.as_bytes())
            box.flush()
        finally:
            box.close()

    sym_cfg = mail_cfg["outside_symlink"]
    outside_secret = outside_dir / sym_cfg["outside_name"]
    sec_msg = EmailMessage()
    sec_msg["From"] = sym_cfg["from"]
    sec_msg["Subject"] = sym_cfg["subject"]
    sec_msg["Date"] = sym_cfg["date"]
    sec_msg.set_content(sym_cfg["body"])
    outside_secret.write_bytes(sec_msg.as_bytes())
    link_path = workspace_dir / sym_cfg["link_path"]
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(outside_secret)

    # 3. Media WAV 생성
    if wav_cfg := seed.get("wav_seed"):
        wav_path = workspace_dir / wav_cfg["path"]
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        sr = int(wav_cfg["sample_rate"])
        total = sr * int(wav_cfg.get("duration_sec", 1))
        freq = float(wav_cfg["freq_hz"])
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            frames = bytearray()
            for i in range(total):
                frames.extend(struct.pack("<h", int(16000 * math.sin(2.0 * math.pi * freq * (i / sr)))))
            wf.writeframes(bytes(frames))

    # 4. Git 히스토리 생성
    if (git_cfg := seed.get("git_seed")) and shutil.which("git"):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": git_cfg["author_name"],
            "GIT_AUTHOR_EMAIL": git_cfg["author_email"],
            "GIT_COMMITTER_NAME": git_cfg["author_name"],
            "GIT_COMMITTER_EMAIL": git_cfg["author_email"],
        }
        subprocess.run(["git", "init"], cwd=workspace_dir, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for commit_spec in git_cfg.get("commits", ()):
            for rel_f, code in commit_spec["files"].items():
                fp = workspace_dir / rel_f
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(code, encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=workspace_dir, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "commit", "-m", commit_spec["message"]], cwd=workspace_dir, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return outside_secret


class BuiltinWorkerValidationPipeline(BasePipeline):
    def __init__(self, config: E2EConfig):
        super().__init__(
            config=config,
            name="Built-in Workers (Fixture-Driven 7-Target) E2E Suite",
            scope_name="BUILTIN_WORKER_VALIDATION",
        )
        self.dpop_client = DPoPClientGenerator(key_size=2048)
        self.run_scope = uuid.uuid4().hex[:6]
        self.targets: Dict[str, str] = {key: f"e2e.{key}.{self.run_scope}" for key, _, _ in TARGET_SPECS}

        self.fixture_dir = resolve_fixture_dir(FIXTURE_SUBDIR, anchor_file=__file__)
        self.seed_data: Dict[str, Any] = json.loads((self.fixture_dir / SEED_FILE).read_text(encoding="utf-8"))
        self.contracts_doc: Dict[str, Any] = json.loads((self.fixture_dir / CONTRACTS_FILE).read_text(encoding="utf-8"))

        self._tmp_ws_ctx: Optional[tempfile.TemporaryDirectory] = None
        self._tmp_out_ctx: Optional[tempfile.TemporaryDirectory] = None
        self.workspace_dir: Path = Path.cwd()
        self.outside_secret_file: Optional[Path] = None

        self.set_phases([
            Phase("Phase 1: 7-Worker Boot, Schema Snapshot & Unified Route", self.phase_discovery),
            Phase("Phase 2: [Duck] Fixture Cases & 10x Async Concurrency", self.phase_duck),
            Phase("Phase 3: [Verifier] Fixture Cases", lambda: self._run_contract_suite("verifier")),
            Phase("Phase 4: [Mailbox] Fixture Cases & Symlink Escape Audit", lambda: self._run_contract_suite("mailbox")),
            Phase("Phase 5: [Media] Fixture Cases (FFprobe/FFmpeg)", lambda: self._run_contract_suite("media")),
            Phase("Phase 6: [Reader] Partial-Sync Guard (#6) & Scoped Retrieval", lambda: self._run_contract_suite("reader")),
            Phase("Phase 7: [Pin] Full Lifecycle Fixture Cases (#12)", lambda: self._run_contract_suite("pin")),
            Phase("Phase 8: [GitHistory] Commit/Blame/Show Fixture Cases", lambda: self._run_contract_suite("git")),
            Phase("Phase 9: [Cross-Worker] 11-Case Error Contract Audit (#8)", lambda: self._run_contract_suite("error_contracts")),
        ])

    # ------------------------------------------------------------------ HTTP & Suite Snapshot Engine
    async def _invoke(
        self, target_id: str, tool_name: str, arguments: Dict[str, Any], *, unified: bool = False, req_id: int = 1
    ) -> httpx.Response:
        url = (
            f"{self.local_url}/v1/mcp-gateway/invoke"
            if unified
            else f"{self.local_url}/v1/mcp-gateway/{target_id}/invoke"
        )
        wire_name = f"{target_id}__{tool_name}" if unified else tool_name
        payload = {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": wire_name, "arguments": arguments}}
        nonce = uuid.uuid4().hex
        headers = {
            "x-idempotency-key": uuid.uuid4().hex,
            "x-nonce": nonce,
            "DPoP": self.dpop_client.generate_proof(url=url, method="POST", nonce=nonce),
        }
        async with httpx.AsyncClient(base_url=self.local_url, timeout=15.0) as client:
            return await client.post(url, json=payload, headers=headers)

    def _substitute_vars(self, obj: Any) -> Any:
        if isinstance(obj, str) and self.outside_secret_file is not None:
            return obj.replace("$OUTSIDE_SECRET", self.outside_secret_file.as_posix())
        if isinstance(obj, Mapping):
            return {k: self._substitute_vars(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._substitute_vars(v) for v in obj]
        return obj

    def _build_snapshot_state(self, res: httpx.Response) -> Tuple[str, Dict[str, Any]]:
        if res.status_code != 200:
            return f"HTTP_{res.status_code}", {"errors": [res.text]}
        body = res.json()
        if "error" in body:
            return "JSONRPC_ERROR", {"errors": [json.dumps(body["error"])]}
        result = body.get("result", {})
        content = result.get("content", [])
        raw_text = content[0].get("text", "") if (content and isinstance(content[0], dict)) else ""
        if result.get("isError") is True:
            return "TOOL_ERROR_200", {"errors": [raw_text], "_raw": raw_text}
        parsed: Dict[str, Any] = {"_raw": raw_text}
        if raw_text:
            try:
                val = json.loads(raw_text)
                if isinstance(val, Mapping):
                    parsed.update(val)
            except json.JSONDecodeError:
                pass
        return "OK", parsed

    async def _run_contract_suite(self, suite_name: str) -> None:
        base_defaults = self.contracts_doc.get("defaults") or {}
        sec = self.contracts_doc["suites"][suite_name]
        req_bins = sec.get("requires_bins") or ()
        if req_bins and not all(shutil.which(b) for b in req_bins):
            log.info(f"  └─ Skipping suite '{suite_name}' (missing host binary: {req_bins})")
            return

        defaults = {**base_defaults, **(sec.get("defaults") or {})}
        comparators = {
            "raw_contains": lambda st, needles: next(
                (f"raw_contains: missing {n!r} in {st.get('_raw', '')[:160]!r}" for n in needles if n not in st.get("_raw", "")),
                None,
            ),
            "file_exists_key": lambda st, k: (
                None
                if (rel := st.get(k)) and (self.workspace_dir / rel).is_file()
                else f"file_exists_key({k}): output file missing on disk"
            ),
            "non_empty_key": lambda st, k: None if st.get(k) else f"non_empty_key({k}): value was empty",
        }

        failures: List[str] = []
        for idx, raw_case in enumerate(sec.get("cases", ())):
            case = {**defaults, **raw_case}
            cid = f"{suite_name}.{case.get('id', f'case_{idx}')}"
            tid = self.targets[case["worker"]]
            args = self._substitute_vars(case.get("args") or {})
            is_unified = case.get("route") == "unified"

            res = await self._invoke(tid, case["tool"], args, unified=is_unified, req_id=idx + 1)
            act_kind, state = self._build_snapshot_state(res)
            exp_kind = case.get("expect_kind", "OK")

            if act_kind != exp_kind:
                failures.append(f"{cid}: expected status {exp_kind}, got {act_kind} ({state.get('errors') or state.get('_raw', '')[:120]})")
                continue

            if expect_map := case.get("expect"):
                errs = diff_snapshot(state, expect_map, prefix=cid, strict_keys=False, custom_comparators=comparators)
                failures.extend(errs)

            log.info(f"  ├─ [{cid:40s}] -> {act_kind}")

        if failures:
            raise RuntimeError(f"Suite '{suite_name}' failed:\n  - " + "\n  - ".join(failures))

    # ------------------------------------------------------------------ Lifecycle & Custom Phases
    async def setup_custom_context(self) -> None:
        self._tmp_ws_ctx = tempfile.TemporaryDirectory(prefix="fiber_e2e_ws_")
        self._tmp_out_ctx = tempfile.TemporaryDirectory(prefix="fiber_e2e_out_")
        self.workspace_dir = Path(self._tmp_ws_ctx.name).resolve()
        outside_dir = Path(self._tmp_out_ctx.name).resolve()
        self.outside_secret_file = _materialize_seed(self.seed_data, self.workspace_dir, outside_dir)
        log.info(f"[E2E Setup] Seed loaded from {self.fixture_dir} -> {self.workspace_dir}")

    async def setup_workers(self) -> None:
        assert self._tmp_out_ctx is not None
        quoted_ws = shlex.quote(str(self.workspace_dir))
        db_dir = Path(self._tmp_out_ctx.name).resolve() / "dbs"
        db_dir.mkdir(parents=True, exist_ok=True)

        custom_targets: Dict[str, Tuple[str, str]] = {}
        preflight_off: Dict[str, None] = {}

        for key, canonical_id, mod_path in TARGET_SPECS:
            scoped_id = self.targets[key]
            base_cmd, default_mode, _ = E2EWorkerManager.resolve_worker_defaults(canonical_id)
            cmd_parts = [base_cmd or f"{sys.executable} -m {mod_path}"]
            if _supports_cli_flag(mod_path, "--root"):
                cmd_parts.append(f"--root {quoted_ws}")
            if _supports_cli_flag(mod_path, "--db"):
                cmd_parts.append(f"--db {shlex.quote(str(db_dir / f'{key}.db'))}")

            custom_targets[scoped_id] = (" ".join(cmd_parts), default_mode)
            preflight_off[scoped_id] = None

        E2EWorkerManager.mount_custom_targets(
            self, custom_targets, preflight_overrides=preflight_off, tag="E2E-Isolated"
        )

    async def teardown_custom(self) -> None:
        if self._tmp_ws_ctx:
            self._tmp_ws_ctx.cleanup()
        if self._tmp_out_ctx:
            self._tmp_out_ctx.cleanup()

    async def phase_discovery(self) -> None:
        exp_tools = self.contracts_doc["suites"]["discovery"]["expected_tools"]
        actual_tools: Dict[str, List[str]] = {}
        async with httpx.AsyncClient(base_url=self.local_url, timeout=10.0) as client:
            for key, _, _ in TARGET_SPECS:
                tid = self.targets[key]
                res = await client.get(f"{self.local_url}/v1/mcp-gateway/{tid}/tools")
                if res.status_code != 200:
                    raise RuntimeError(f"Discovery HTTP {res.status_code} for {key}")
                actual_tools[key] = sorted(t["name"] for t in res.json().get("result", {}).get("tools", []))

        errs = diff_snapshot(actual_tools, exp_tools, prefix="discovery.tools", strict_keys=True)
        if errs:
            raise RuntimeError("Tool schema mismatch:\n  - " + "\n  - ".join(errs))

        await self._run_contract_suite("discovery")

    async def phase_duck(self) -> None:
        await self._run_contract_suite("duck")
        # 10개 병렬 동시성 스트레스 테스트 유지
        duck_id = self.targets["duck"]
        par_res = await asyncio.gather(*[
            self._invoke(duck_id, "query_sql", {"sql": "SELECT SUM(latency_ms) AS total_ms FROM 'metrics.csv'"}, req_id=100 + i)
            for i in range(10)
        ])
        for idx, r in enumerate(par_res):
            kind, st = self._build_snapshot_state(r)
            if kind != "OK" or st.get("rows", [{}])[0].get("total_ms") != 57:
                raise RuntimeError(f"Duck concurrency failure at #{idx}: {kind} {st}")


class ValidationSuiteRunner:
    def __init__(self) -> None:
        self.results: List[TestResult] = []

    async def execute(self) -> None:
        cfg = E2EConfig(host="127.0.0.1", port=get_free_port(8360), protocol="http")
        self.results.extend(await BuiltinWorkerValidationPipeline(config=cfg).run_pipeline())
        all_passed = all(r.passed for r in self.results)

        lines = [
            "\n" + "=" * 90,
            "[BUILT-IN WORKERS (FIXTURE-DRIVEN 7-TARGET) E2E VALIDATION REPORT]",
            "=" * 90,
        ]
        for idx, r in enumerate(self.results, 1):
            lines.append(f"{'[O]' if r.passed else '[X]'} {idx:02d}. {r.scenario.ljust(72)} | {'PASSED' if r.passed else 'FAILED'}")
        lines.extend(["-" * 90, "ALL PHASES PASSED." if all_passed else "VALIDATION ISSUES DETECTED.", "=" * 90])
        print("\n".join(lines), flush=True)

        if not all_passed:
            raise SystemExit(1)

def main(args_list: Optional[List[str]] = None) -> None:
    PhaseReactor.ignite(main_coro_func=ValidationSuiteRunner().execute)

if __name__ == "__main__":
    main()