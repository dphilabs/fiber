# fiber.studio.cli.reflex.flow
## @lineage: theoria.reflex.flow
"""Single CLI entrypoint for `UnitsManager`, `BlueprintAnalyzer`, `context`, and `compass`.

Usage:
  python -m reflex.flow                     # [sync]    index gen + static wiring check
  python -m reflex.flow verify [FLOW]       # [verify]  index gen + trace (--hybrid)
  python -m reflex.flow pack <FLOW>         # [pack]    index gen + emit LLM markdown
  python -m reflex.flow compass [--suite]   # [compass] L1-L5 layer + coverage check
  python -m reflex.flow inspect [FLOW]      # [inspect] flow list or ASL node view
  python -m reflex.flow new <PATH> [--kind py] [--name foo] [--desc "..."] [--force]
  python -m reflex.flow build | fix | init | clean
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from fiber.node.reflex.unit.loader import UnitLoaderError
from fiber.node.reflex.unit.manager import UnitsManager
from fiber.studio.base.reflex.blueprint import BlueprintAnalyzer
from fiber.studio.base.reflex.compass import run_compass
from fiber.studio.base.reflex.context import ContextError, collect_flow_markdown

PIPELINES: dict[str, tuple[str, list[str]]] = {
    "sync":    ("인덱스 갱신 + 전체 플로우 정적 와이어링 리포트", ["index_gen", "static_check"]),
    "verify":  ("인덱스 갱신 + 시나리오 트레이스 (dry-run / --hybrid)", ["index_gen", "trace"]),
    "pack":    ("인덱스 갱신 + 대상 플로우 LLM 컨텍스트(stdout) 출력", ["index_gen", "emit_context"]),
    "compass": ("엔진 아키텍처(L1~L5) 및 커버리지 점검 (--suite 지원)", ["arch_compass"]),
    "inspect": ("플로우 목록 또는 대상 플로우 ASL 바인딩 조회", ["inspect"]),
}

REPO_ACTIONS: dict[str, str] = {
    "new":   "신규 유닛/어댑터 스캐폴딩 생성 후 인덱스 갱신",
    "build": "인덱스 갱신 후 isolated 유닛 실행 환경 빌드",
    "fix":   "저장소 기본 디렉터리 레이아웃 보정",
    "init":  "신규 reflexunits 저장소 초기화",
    "clean": "유닛 로더 로컬 실행 캐시 정리",
}


def _run_step(step: str, args: argparse.Namespace, root: Path) -> int:
    if step == "index_gen":
        return UnitsManager(root).gen()

    if step == "arch_compass":
        _, report = run_compass(root, run_verify=args.suite)
        print(report)
        return 0

    analyzer = BlueprintAnalyzer(repo_arg=root, spec_arg=args.spec)

    if step == "static_check":
        print(analyzer.evaluate_readiness())
        return 0

    if step == "trace":
        targets = analyzer.resolve_targets(args.target)
        results = [analyzer.run_trace(f, hybrid=args.hybrid) for f in targets]
        print(json.dumps(results[0] if len(results) == 1 else results, indent=2, ensure_ascii=False))
        return 0

    if step == "inspect":
        if not args.target:
            print(analyzer.format_list())
            return 0
        for fname in analyzer.resolve_targets(args.target):
            print(analyzer.inspect_workflow(fname))
        return 0

    if step == "emit_context":
        matched = analyzer.resolve_targets(args.target) if analyzer.flow_names else [args.target.strip()]
        if len(matched) != 1:
            raise ValueError(f"'pack' requires a single flow, matched: {matched}")
        res = collect_flow_markdown(root, matched[0], live=False)
        markdown, notes = (res[1], res[2]) if len(res) == 3 else res
        for note in notes:
            print(f"[CTX] {note}", file=sys.stderr)
        sys.stdout.write(markdown)
        return 0

    raise ValueError(f"Unknown step: {step}")


def _run_repo_action(cmd: str, args: argparse.Namespace, root: Path) -> int:
    mgr = UnitsManager(root, allow_create=(cmd == "init"))
    if cmd == "new":
        return mgr.new(args.target, unit_name=args.name, kind=args.kind, desc=args.desc, overwrite=args.force)
    return getattr(mgr, cmd)()


def main(argv: list[str] | None = None) -> int:
    all_cmds = {**{k: v[0] for k, v in PIPELINES.items()}, **REPO_ACTIONS}
    epilog = "\n".join(f"  {k:<10} {desc}" for k, desc in all_cmds.items())

    parser = argparse.ArgumentParser(
        prog="python -m reflex.flow",
        epilog=f"commands:\n{epilog}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("cmd", nargs="?", default="sync", choices=list(all_cmds))
    parser.add_argument("target", nargs="?", default="", help="Flow name/index or relative path (for 'new')")
    parser.add_argument("--root", default="reflexunits", help="Path to reflexunits repository root")
    parser.add_argument("--spec", default="", help="External blueprint JSON override")
    parser.add_argument("--hybrid", action="store_true", help="Run real local py/exec units on 'verify'")
    parser.add_argument("--suite", action="store_true", help="Run FSM verification suites on 'compass'")
    parser.add_argument("--name", default="", help="@unit name override for 'new'")
    parser.add_argument("--kind", default="auto", choices=["auto", "exec", "py", "worker", "adapter"])
    parser.add_argument("--desc", default="TODO: description")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd in ("pack", "new") and not args.target:
        print(f"[FLOW:ERR] '{args.cmd}' requires a target.", file=sys.stderr)
        return 1

    root = Path(args.root).resolve()
    try:
        if args.cmd in REPO_ACTIONS:
            return _run_repo_action(args.cmd, args, root)

        _, steps = PIPELINES[args.cmd]
        for step in steps:
            rc = _run_step(step, args, root)
            if rc != 0:
                return rc
    except (ContextError, UnitLoaderError, KeyError, ValueError, RuntimeError) as exc:
        print(f"[FLOW:ERR] {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())