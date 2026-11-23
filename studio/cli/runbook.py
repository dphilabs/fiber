# fiber.studio.cli.runbook
"""
Usage:
  python -m fiber.studio.cli.runbook                     # [sync]    index gen + static wiring check
  python -m fiber.studio.cli.runbook check               # [check]   verify index sync & adapter rules (CI)
  python -m fiber.studio.cli.runbook verify [FLOW]       # [verify]  index gen + trace (--hybrid)
  python -m fiber.studio.cli.runbook pack <FLOW>         # [pack]    index gen + emit LLM markdown (stdout)
  python -m fiber.studio.cli.runbook inspect [FLOW]      # [inspect] flow list or ASL node view
  python -m fiber.studio.cli.runbook new <PATH> [--kind py] [--name foo] [--desc "..."] [--force]
  python -m fiber.studio.cli.runbook build | fix | init | clean | compass
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
import tomllib
from typing import Any

from xphi.arch.reflex.script import PEP723_MARKER, has_pep723

from fiber.node.runbook.unit import loader as unit_loader_mod
from fiber.node.runbook.unit.indexer import UnitsIndexer
from fiber.node.runbook.unit.loader import UnitLoaderError, UnitsLoader
from fiber.studio.base.reflex.blueprint import BlueprintAnalyzer
from fiber.studio.base.reflex.context import ContextError, collect_flow_markdown

UNIT_TEMPLATE_DIR = Path(unit_loader_mod.__file__).resolve().parent / "templates"

_FORBIDDEN_ADAPTER_ROOTS = frozenset({"reflexor", "theoria"})
_ALLOWED_ADAPTER_PREFIX = "reflexor.units.adapters"
_SHARED_ADAPTER_DOMAINS = frozenset({"auth"})

# 모든 CLI 서브커맨드를 단일 스텝 파이프라인으로 통합 정의
COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "sync":    ("인덱스 갱신 + 전체 플로우 정적 와이어링 리포트", ("index_gen", "static_check")),
    "check":   ("index.json 정합성 및 어댑터 아키텍처 경계 무결성 검사", ("repo_check",)),
    "verify":  ("인덱스 갱신 + 시나리오 트레이스 (dry-run / --hybrid)", ("index_gen", "trace")),
    "pack":    ("인덱스 갱신 + 대상 플로우 LLM 컨텍스트(stdout) 출력", ("index_gen", "emit_context")),
    "inspect": ("플로우 목록 또는 대상 플로우 ASL 바인딩 조회", ("inspect",)),
    "compass": ("엔진 아키텍처(L1~L5) 및 커버리지 점검 (--suite 지원)", ("arch_compass",)),
    "init":    ("신규 reflexor 저장소 초기화 및 인덱스 생성", ("repo_init", "index_gen")),
    "new":     ("신규 유닛/어댑터 스캐폴딩 생성 후 인덱스 갱신", ("scaffold_unit", "index_gen")),
    "build":   ("인덱스 갱신 후 isolated 유닛 실행 환경 빌드", ("index_gen", "env_build")),
    "fix":     ("저장소 기본 디렉터리 레이아웃 보정", ("repo_fix",)),
    "clean":   ("유닛 로더 로컬 실행 캐시 정리", ("repo_clean",)),
}


def _note(msg: str) -> None:
    """진행 로그 및 경고를 stderr로 출력해 `pack`/`verify`의 stdout 파이프 오염 방지."""
    print(msg, file=sys.stderr)


# =====================================================================
# 1. Adapter & Repository Integrity Checks (순수 검증 함수)
# =====================================================================

def _check_pyproject_adapters(pyproject: Path) -> str | None:
    """`pyproject.toml`이 유효한 TOML이며 패키지/wheel 설정이 `reflexor`/`adapters`를 포함하는지 검증."""
    if not pyproject.is_file():
        return "[PKG-ADAPTER] Missing `pyproject.toml` at repo root (required to import `reflexor.units.adapters.*`)."
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return f"[PKG-ADAPTER] Invalid `pyproject.toml`: {exc}"

    raw_dump = json.dumps(data.get("tool", {}))
    if not any(tok in raw_dump for tok in ("adapters", "reflexor", "units")):
        return "[PKG-ADAPTER] `pyproject.toml` package/wheel config does not cover `units/adapters/**`."
    return None


def _resolve_import_targets(rel_py: str, node: ast.AST) -> list[tuple[int, str]]:
    """절대 import 및 상대 import(`from .. import x`)를 모두 정규 모듈 경로로 변환."""
    if isinstance(node, ast.Import):
        return [(node.lineno, alias.name) for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []

    if node.level == 0:
        return [(node.lineno, node.module)] if node.module else []

    rel_parts = list(Path(rel_py).with_suffix("").parts)
    if rel_parts and rel_parts[-1] == "__init__":
        rel_parts.pop()
    pkg_parts = ["reflexor", *rel_parts[:-1]]

    up = node.level - 1
    if up > len(pkg_parts):
        return [(node.lineno, "<escaped-repo-root>")]

    base_parts = pkg_parts[: len(pkg_parts) - up]
    if node.module:
        return [(node.lineno, ".".join([*base_parts, node.module]))]
    return [(node.lineno, ".".join([*base_parts, alias.name])) for alias in node.names]


def _is_forbidden_adapter_import(
    target_mod: str,
    owner_domain: str,
    *,
    strict_cross_domain: bool = False,
) -> str | None:
    """위반 시 사유 문자열을 반환하고, 허용된 import이면 None 반환."""
    if target_mod == "<escaped-repo-root>":
        return "relative import escapes repository root"

    if target_mod == _ALLOWED_ADAPTER_PREFIX or target_mod.startswith(f"{_ALLOWED_ADAPTER_PREFIX}."):
        if not strict_cross_domain:
            return None
        sub = target_mod.removeprefix(f"{_ALLOWED_ADAPTER_PREFIX}.").split(".", 1)[0]
        if sub in {owner_domain, *_SHARED_ADAPTER_DOMAINS}:
            return None
        return f"cross-adapter dependency `{owner_domain}` -> `{sub}` is not allowed"

    root_pkg = target_mod.split(".", 1)[0]
    if root_pkg in _FORBIDDEN_ADAPTER_ROOTS:
        return f"`units/adapters` must not depend on `{root_pkg}` outside `{_ALLOWED_ADAPTER_PREFIX}`"
    return None


def _validate_adapters(
    repo_root: Path,
    adapters_map: dict[str, list[str]],
    *,
    strict_cross_domain: bool = False,
) -> list[str]:
    """`units/adapters/` 무결성 검사: 패키징, 문법, PEP 723 금지, @unit 혼입 금지, 아키텍처 경계 검증."""
    errors: list[str] = []
    if not adapters_map:
        return errors

    if pkg_err := _check_pyproject_adapters(repo_root / "pyproject.toml"):
        errors.append(pkg_err)

    for domain, rel_files in sorted(adapters_map.items()):
        for rel_py in sorted(rel_files):
            file_path = repo_root / rel_py
            if not file_path.is_file():
                errors.append(f"[MISSING-ADAPTER] '{rel_py}' listed in adapters map does not exist.")
                continue

            src = file_path.read_text(encoding="utf-8")
            if has_pep723(src):
                errors.append(
                    f"[BAD-ADAPTER] '{rel_py}' contains `{PEP723_MARKER}`; "
                    "adapters must run as shared package modules, not isolated scripts."
                )

            try:
                tree = ast.parse(src, filename=rel_py)
            except SyntaxError as exc:
                errors.append(f"[SYNTAX-ERR] '{rel_py}:{exc.lineno}': {exc.msg}")
                continue

            doc = ast.get_docstring(tree) or ""
            if any(line.strip().startswith(("@unit:", "@flow:", "@flow.")) for line in doc.splitlines()):
                errors.append(
                    f"[BAD-ADAPTER] '{rel_py}' declares `@unit`/`@flow`; "
                    "move unit entrypoints to `units/fiber/` and keep `units/adapters/` pure."
                )

            seen_violations: set[tuple[int, str]] = set()
            for node in ast.walk(tree):
                for lineno, mod_target in _resolve_import_targets(rel_py, node):
                    if reason := _is_forbidden_adapter_import(
                        mod_target, domain, strict_cross_domain=strict_cross_domain
                    ):
                        key = (lineno, mod_target)
                        if key not in seen_violations:
                            seen_violations.add(key)
                            errors.append(
                                f"[ARCH-BOUNDARY] '{rel_py}:{lineno}' imports `{mod_target}` — {reason}."
                            )

    return errors


def _audit_index_sync(repo_root: Path, live_index: dict[str, Any], *, verify_saved_file: bool) -> list[str]:
    adapters_meta = live_index.get("adapters", {})
    issues = _validate_adapters(repo_root, adapters_meta)

    if verify_saved_file:
        idx_path = repo_root / UnitsIndexer.INDEX_FILENAME
        if not idx_path.is_file():
            issues.append(f"[MISSING-INDEX] {idx_path.name} does not exist (run `sync`).")
        else:
            saved = json.loads(idx_path.read_text(encoding="utf-8"))
            if any(saved.get(k, {}) != live_index.get(k, {}) for k in ("units", "flows", "adapters")):
                issues.append("[STALE-INDEX] index.json is out of sync with current repository state (run `sync`).")

    return issues


# =====================================================================
# 2. Unified CLI Execution Session (UnitsManager + Pipeline Runner 통합)
# =====================================================================

class RunbookSession:
    """저장소 유닛 관리(Loader/Indexer)와 플로우 블루프린트 분석을 조율하는 통합 세션."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        spec_arg: str = "",
        template_dir: str | Path = UNIT_TEMPLATE_DIR,
        allow_create: bool = False,
    ) -> None:
        self.loader = UnitsLoader(
            local_repo_dir=repo_root,
            template_dir=template_dir,
            allow_create=allow_create,
        )
        self.root = self.loader.root
        self.indexer = UnitsIndexer(self.loader)
        self.spec_arg = spec_arg
        self._analyzer: BlueprintAnalyzer | None = None
        self._last_index: dict[str, Any] | None = None

    @property
    def analyzer(self) -> BlueprintAnalyzer:
        if self._analyzer is None:
            self._analyzer = BlueprintAnalyzer(repo_arg=self.root, spec_arg=self.spec_arg)
        return self._analyzer

    def compose_index(self) -> dict[str, Any]:
        data = self.indexer.build_repo_index(self.root)
        data["adapters"] = self.loader.discover_adapters()
        if head_sha := self.loader.git_head_sha():
            data["commit"] = head_sha
        self._last_index = data
        return data

    # --- Repository & Unit Lifecycle Steps ---

    def step_repo_init(self, _args: argparse.Namespace) -> int:
        self.loader.ensure_layout()
        self.loader.fix_repo()
        return 0

    def step_scaffold_unit(self, args: argparse.Namespace) -> int:
        self.loader.scaffold(
            args.target,
            unit_name=args.name,
            kind=args.kind,
            desc=args.desc,
            overwrite=args.force,
        )
        return 0

    def step_index_gen(self, _args: argparse.Namespace) -> int:
        data = self.compose_index()
        idx_path = self.root / UnitsIndexer.INDEX_FILENAME
        idx_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _note(
            f"[GEN] Wrote catalog index -> {idx_path} "
            f"({len(data.get('units', {}))} units, {len(data.get('adapters', {}))} adapters, {len(data.get('flows', {}))} flows)"
        )
        for warn in _audit_index_sync(self.root, data, verify_saved_file=False):
            _note(f"  {warn}")
        self._analyzer = None  # 갱신된 인덱스를 후속 스텝이 다시 읽도록 캐시 초기화
        return 0

    def step_repo_check(self, _args: argparse.Namespace) -> int:
        data = self.compose_index()
        issues = _audit_index_sync(self.root, data, verify_saved_file=True)
        for err in issues:
            _note(f"  {err}")
        return 1 if issues else 0

    def step_env_build(self, _args: argparse.Namespace) -> int:
        idx_data = self._last_index or self.compose_index()
        isolated = [
            m["path"]
            for m in idx_data.get("units", {}).values()
            if isinstance(m, dict) and m.get("isolated")
        ]
        return self.loader.build_environments(isolated)

    def step_repo_fix(self, _args: argparse.Namespace) -> int:
        return self.loader.fix_repo()

    def step_repo_clean(self, _args: argparse.Namespace) -> int:
        return self.loader.clean_cache()

    # --- Blueprint & Workflow Inspection Steps ---

    def step_static_check(self, _args: argparse.Namespace) -> int:
        print(self.analyzer.evaluate_readiness())
        return 0

    def step_trace(self, args: argparse.Namespace) -> int:
        targets = self.analyzer.resolve_targets(args.target)
        results = [self.analyzer.run_trace(f, hybrid=args.hybrid) for f in targets]
        print(json.dumps(results[0] if len(results) == 1 else results, indent=2, ensure_ascii=False))
        return 0

    def step_inspect(self, args: argparse.Namespace) -> int:
        if not args.target:
            print(self.analyzer.format_list())
            return 0
        for fname in self.analyzer.resolve_targets(args.target):
            print(self.analyzer.inspect_workflow(fname))
        return 0

    def step_emit_context(self, args: argparse.Namespace) -> int:
        matched = self.analyzer.resolve_targets(args.target) if self.analyzer.flow_names else [args.target.strip()]
        if len(matched) != 1:
            raise ValueError(f"'pack' requires a single flow, matched: {matched}")
        res = collect_flow_markdown(self.root, matched[0], live=False)
        markdown, notes = (res[1], res[2]) if len(res) == 3 else res
        for note in notes:
            _note(f"[CTX] {note}")
        sys.stdout.write(markdown)
        return 0

    def step_arch_compass(self, args: argparse.Namespace) -> int:
        if hasattr(self.analyzer, "evaluate_compass"):
            print(self.analyzer.evaluate_compass(run_suite=args.suite))
            return 0
        raise RuntimeError("'compass' handler is not bound on BlueprintAnalyzer.")

    def run_pipeline(self, steps: tuple[str, ...], args: argparse.Namespace) -> int:
        for step in steps:
            handler = getattr(self, f"step_{step}", None)
            if handler is None:
                raise ValueError(f"Unknown pipeline step: {step!r}")
            rc = handler(args)
            if rc != 0:
                return rc
        return 0


# =====================================================================
# 3. CLI Entrypoint
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    epilog = "\n".join(f"  {cmd:<10} {desc}" for cmd, (desc, _) in COMMANDS.items())
    parser = argparse.ArgumentParser(
        prog="python -m fiber.studio.cli.runbook",
        epilog=f"commands:\n{epilog}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("cmd", nargs="?", default="sync", choices=list(COMMANDS))
    parser.add_argument("target", nargs="?", default="", help="Flow name/index or relative path (for 'new')")
    parser.add_argument("--root", default="reflexor", help="Path to reflexor repository root")
    parser.add_argument("--spec", default="", help="External blueprint JSON override")
    parser.add_argument("--hybrid", action="store_true", help="Run real local py/exec units on 'verify'")
    parser.add_argument("--suite", action="store_true", help="Run FSM verification suites on 'compass'")
    parser.add_argument("--name", default="", help="@unit name override for 'new'")
    parser.add_argument("--kind", default="auto", choices=["auto", "exec", "py", "worker", "adapter"])
    parser.add_argument("--desc", default="TODO: description")
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd in ("pack", "new") and not args.target:
        _note(f"[FLOW:ERR] '{args.cmd}' requires a target.")
        return 1

    root = Path(args.root).resolve()
    try:
        session = RunbookSession(
            root,
            spec_arg=args.spec,
            allow_create=(args.cmd == "init"),
        )
        _, steps = COMMANDS[args.cmd]
        return session.run_pipeline(steps, args)
    except (ContextError, UnitLoaderError, KeyError, ValueError, RuntimeError) as exc:
        _note(f"[FLOW:ERR] {exc}")
        return 1

if __name__ == "__main__":
    sys.exit(main())