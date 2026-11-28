# fiber.studio.cli.runbook
"""
CLI pipeline for Reflexor repository indexing, static verification, and context export.

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

from xphi.arch.contract.registry.flow import parse_flow_dsl, parse_unit_dsl
from xphi.arch.reflex.script import PEP723_MARKER, has_pep723

from fiber.studio.harness.reflex.blueprint import BlueprintAnalyzer
from fiber.studio.harness.reflex.context import ContextError, collect_flow_markdown
from fiber.studio.runbook.unit import loader as unit_loader_mod
from fiber.studio.runbook.unit.indexer import UnitsIndexer, expand_entries, is_flow_meta
from fiber.studio.runbook.unit.loader import UnitLoaderError, UnitsLoader

UNIT_TEMPLATE_DIR = Path(unit_loader_mod.__file__).resolve().parent / "templates"

_FORBIDDEN_ADAPTER_ROOTS = frozenset({"reflexor", "theoria"})
_ALLOWED_ADAPTER_PREFIX = "reflexor.units.adapters"
_SHARED_ADAPTER_DOMAINS = frozenset({"auth"})

COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "sync":    ("Refresh index.json and print static wiring report", ("index_gen", "static_check")),
    "check":   ("Verify index.json sync and adapter boundary rules", ("repo_check",)),
    "verify":  ("Refresh index and run scenario trace (dry-run / --hybrid)", ("index_gen", "trace")),
    "pack":    ("Refresh index and emit flow LLM context to stdout", ("index_gen", "emit_context")),
    "inspect": ("List flows or inspect compiled ASL node bindings", ("inspect",)),
    "compass": ("Evaluate L1-L5 engine architecture and suite coverage", ("arch_compass",)),
    "init":    ("Initialize reflexor repository layout and index", ("repo_init", "index_gen")),
    "new":     ("Scaffold a unit/adapter template and refresh index", ("scaffold_unit", "index_gen")),
    "build":   ("Refresh index and build isolated PEP 723 environments", ("index_gen", "env_build")),
    "fix":     ("Repair repository directory layout", ("repo_fix",)),
    "clean":   ("Clean local unit loader execution caches", ("repo_clean",)),
}


def _note(msg: str) -> None:
    """Write status and diagnostics to stderr so stdout stays clean for piping."""
    print(msg, file=sys.stderr)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _meta_loc(meta: Any, fallback: str) -> str:
    if isinstance(meta, dict):
        return str(meta.get("path") or meta.get("source") or fallback)
    return fallback


# =====================================================================
# 1. Adapter, Contract & Repository Integrity Checks
# =====================================================================

def _check_pyproject_adapters(pyproject: Path) -> str | None:
    """Ensure `pyproject.toml` is valid and packages `reflexor.units.adapters`."""
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
    """Normalize absolute and relative imports into canonical dotted module paths."""
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
    """Return violation reason if `target_mod` breaks adapter isolation rules."""
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
    """Audit `units/adapters/`: packaging, syntax, no PEP 723, no @unit/@flow, and import boundaries."""
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


def _partition_valid_contracts(
    units_map: dict[str, Any],
    flows_map: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Parse all unit and flow DSL entries; separate valid entries from `[BAD-UNIT]`/`[BAD-FLOW]` errors."""
    valid_units: dict[str, Any] = {}
    valid_flows: dict[str, Any] = {}
    errors: list[str] = []

    for u_name, u_meta in sorted(units_map.items()):
        if not is_flow_meta(u_meta):
            valid_units[u_name] = u_meta
            continue
        src_loc = _meta_loc(u_meta, "units")
        bad = False
        for key, spec, desc in expand_entries(u_name, u_meta):
            try:
                parse_unit_dsl(key, spec, desc)
            except ValueError as exc:
                errors.append(f"[BAD-UNIT] '{key}' ({src_loc}): {exc}")
                bad = True
        if not bad:
            valid_units[u_name] = u_meta

    for f_name, f_meta in sorted(flows_map.items()):
        if not is_flow_meta(f_meta):
            continue
        src_loc = _meta_loc(f_meta, "flow/*.json")
        bad = False
        for key, spec, desc in expand_entries(f_name, f_meta):
            try:
                parse_flow_dsl(key, spec, desc)
            except ValueError as exc:
                errors.append(f"[BAD-FLOW] '{key}' ({src_loc}): {exc}")
                bad = True
        if not bad:
            valid_flows[f_name] = f_meta

    return valid_units, valid_flows, errors


def _audit_index_sync(repo_root: Path, live_index: dict[str, Any], *, verify_saved_file: bool) -> list[str]:
    issues = _validate_adapters(repo_root, live_index.get("adapters", {}))
    _, _, dsl_errors = _partition_valid_contracts(
        live_index.get("units", {}),
        live_index.get("flows", {}),
    )
    issues.extend(dsl_errors)

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
# 2. Unified CLI Execution Session
# =====================================================================

class RunbookSession:
    """Coordinates repository unit indexing, environment builds, and blueprint analysis."""

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
        self._had_dsl_errors = False

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
        raw_data = self.compose_index()
        valid_units, valid_flows, dsl_errors = _partition_valid_contracts(
            raw_data.get("units", {}),
            raw_data.get("flows", {}),
        )
        self._had_dsl_errors = bool(dsl_errors)

        # Quarantine invalid DSL entries from index.json to keep console/analyzers bootable
        clean_data = {**raw_data, "units": valid_units, "flows": valid_flows}
        idx_path = self.root / UnitsIndexer.INDEX_FILENAME
        _atomic_write_json(idx_path, clean_data)

        skipped_note = f", quarantined {len(dsl_errors)} invalid DSL(s)" if dsl_errors else ""
        _note(
            f"[GEN] Wrote catalog index -> {idx_path} "
            f"({len(valid_units)} units, {len(clean_data.get('adapters', {}))} adapters, "
            f"{len(valid_flows)} flows{skipped_note})"
        )
        for err in [*_validate_adapters(self.root, clean_data.get("adapters", {})), *dsl_errors]:
            _note(f"  {err}")

        self._analyzer = None  # Reset analyzer cache so downstream steps read the updated index
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
        return 1 if self._had_dsl_errors else 0

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
        return 1 if self._had_dsl_errors else 0


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