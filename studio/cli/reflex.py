# fiber.studio.cli.reflex
"""CLI pipeline for Reflexor repository indexing, static verification, and context export."""
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

from fiber.studio.reflex import loader as unit_loader_mod
from fiber.studio.reflex.blueprint import BlueprintAnalyzer
from fiber.studio.reflex.flow.catalog import ContextError, collect_flow_markdown
from fiber.studio.reflex.indexer import UnitsIndexer, expand_entries, is_flow_meta
from fiber.studio.reflex.loader import UnitLoaderError, UnitsLoader

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
    print(msg, file=sys.stderr)

def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)

def _resolve_import_targets(rel_py: str, node: ast.AST) -> list[tuple[int, str]]:
    if isinstance(node, ast.Import):
        return [(node.lineno, a.name) for a in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    if node.level == 0:
        return [(node.lineno, node.module)] if node.module else []

    parts = list(Path(rel_py).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    pkg = ["reflexor", *parts[:-1]]
    up = node.level - 1
    if up > len(pkg):
        return [(node.lineno, "<escaped-repo-root>")]
    base = pkg[: len(pkg) - up]
    if node.module:
        return [(node.lineno, ".".join([*base, node.module]))]
    return [(node.lineno, ".".join([*base, a.name])) for a in node.names]

def _forbidden_adapter_reason(target_mod: str, domain: str, strict_cross: bool) -> str | None:
    if target_mod == "<escaped-repo-root>":
        return "relative import escapes repository root"
    if target_mod == _ALLOWED_ADAPTER_PREFIX or target_mod.startswith(f"{_ALLOWED_ADAPTER_PREFIX}."):
        if not strict_cross:
            return None
        sub = target_mod.removeprefix(f"{_ALLOWED_ADAPTER_PREFIX}.").split(".", 1)[0]
        return None if sub in {domain, *_SHARED_ADAPTER_DOMAINS} else f"cross-adapter dependency `{domain}` -> `{sub}` is not allowed"
    root_pkg = target_mod.split(".", 1)[0]
    if root_pkg in _FORBIDDEN_ADAPTER_ROOTS:
        return f"`units/adapters` must not depend on `{root_pkg}` outside `{_ALLOWED_ADAPTER_PREFIX}`"
    return None

def _validate_adapters(repo_root: Path, adapters_map: dict[str, list[str]], *, strict_cross_domain: bool = False) -> list[str]:
    if not adapters_map:
        return []
    errors: list[str] = []
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        errors.append("[PKG-ADAPTER] Missing `pyproject.toml` at repo root.")
    else:
        try:
            raw_tool = json.dumps(tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}))
            if not any(t in raw_tool for t in ("adapters", "reflexor", "units")):
                errors.append("[PKG-ADAPTER] `pyproject.toml` package/wheel config does not cover `units/adapters/**`.")
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"[PKG-ADAPTER] Invalid `pyproject.toml`: {exc}")

    for domain, rel_files in sorted(adapters_map.items()):
        for rel_py in sorted(rel_files):
            fp = repo_root / rel_py
            if not fp.is_file():
                errors.append(f"[MISSING-ADAPTER] '{rel_py}' listed in adapters map does not exist.")
                continue
            src = fp.read_text(encoding="utf-8")
            if has_pep723(src):
                errors.append(f"[BAD-ADAPTER] '{rel_py}' contains `{PEP723_MARKER}`; adapters must be shared modules.")
            try:
                tree = ast.parse(src, filename=rel_py)
            except SyntaxError as exc:
                errors.append(f"[SYNTAX-ERR] '{rel_py}:{exc.lineno}': {exc.msg}")
                continue
            doc = ast.get_docstring(tree) or ""
            if any(line.strip().startswith(("@unit:", "@flow:", "@flow.")) for line in doc.splitlines()):
                errors.append(f"[BAD-ADAPTER] '{rel_py}' declares `@unit`/`@flow`; keep `units/adapters/` pure.")

            seen: set[tuple[int, str]] = set()
            for node in ast.walk(tree):
                for lineno, mod in _resolve_import_targets(rel_py, node):
                    if (lineno, mod) not in seen and (reason := _forbidden_adapter_reason(mod, domain, strict_cross_domain)):
                        seen.add((lineno, mod))
                        errors.append(f"[ARCH-BOUNDARY] '{rel_py}:{lineno}' imports `{mod}` — {reason}.")
    return errors

def _partition_valid_contracts(
    repo_root: Path, units_map: dict[str, Any], flows_map: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    valid_units, valid_flows, errors = {}, {}, []
    spec_cache: dict[str, dict[str, Any]] = {}

    def _spec(rel: str) -> dict[str, Any]:
        if rel not in spec_cache:
            spec_cache[rel] = json.loads((repo_root / rel).read_text(encoding="utf-8"))
        return spec_cache[rel]

    for u_name, u_entry in sorted(units_map.items()):
        try:
            if isinstance(u_entry, dict) and "path" in u_entry:
                loc = u_entry["path"]
                u_meta = UnitsIndexer.parse_script_header((repo_root / loc).read_bytes(), loc)
            elif isinstance(u_entry, dict) and "source" in u_entry:
                loc = u_entry["source"]
                u_meta = (_spec(loc).get("units") or {}).get(u_name)
            else:
                loc, u_meta = "units", u_entry
        except (OSError, ValueError) as exc:
            errors.append(f"[BAD-UNIT] '{u_name}': {exc}")
            continue

        if not is_flow_meta(u_meta):
            continue
        bad = False
        for key, spec, desc in expand_entries(u_name, u_meta):
            try:
                parse_unit_dsl(key, spec, desc)
            except ValueError as exc:
                errors.append(f"[BAD-UNIT] '{key}' ({loc}): {exc}")
                bad = True
        if not bad:
            valid_units[u_name] = u_entry

    for f_name, f_entry in sorted(flows_map.items()):
        loc = f_entry if isinstance(f_entry, str) else f_entry.get("source", "")
        try:
            f_meta = (_spec(loc).get("flows") or {}).get(f_name) if loc.endswith(".json") else f_entry
        except (OSError, ValueError) as exc:
            errors.append(f"[BAD-FLOW] '{f_name}' ({loc}): {exc}")
            continue

        if not is_flow_meta(f_meta):
            continue
        bad = False
        for key, spec, desc in expand_entries(f_name, f_meta):
            try:
                parse_flow_dsl(key, spec, desc)
            except ValueError as exc:
                errors.append(f"[BAD-FLOW] '{key}' ({loc}): {exc}")
                bad = True
        if not bad:
            valid_flows[f_name] = f_entry

    return valid_units, valid_flows, errors


class reflexSession:
    def __init__(self, repo_root: str | Path | None = None, *, spec_arg: str = "", allow_create: bool = False) -> None:
        self.loader = UnitsLoader(local_repo_dir=repo_root, template_dir=UNIT_TEMPLATE_DIR, allow_create=allow_create)
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
        if sha := self.loader.git_head_sha():
            data["commit"] = sha
        self._last_index = data
        return data

    def step_repo_init(self, _args: argparse.Namespace) -> int:
        self.loader.ensure_layout()
        return self.loader.fix_repo()

    def step_scaffold_unit(self, args: argparse.Namespace) -> int:
        self.loader.scaffold(args.target, unit_name=args.name, kind=args.kind, desc=args.desc, overwrite=args.force)
        return 0

    def step_index_gen(self, _args: argparse.Namespace) -> int:
        raw = self.compose_index()
        valid_units, valid_flows, dsl_errors = _partition_valid_contracts(self.root, raw.get("units", {}), raw.get("flows", {}))
        self._had_dsl_errors = bool(dsl_errors)

        clean = {k: v for k, v in raw.items() if k != "flows"}
        clean["units"] = valid_units
        if valid_flows:
            clean["flows"] = valid_flows

        idx_path = self.root / UnitsIndexer.INDEX_FILENAME
        _atomic_write_json(idx_path, clean)
        skip = f", quarantined {len(dsl_errors)} invalid DSL(s)" if dsl_errors else ""
        _note(f"[GEN] Wrote catalog index -> {idx_path} ({len(valid_units)} units, {len(clean.get('adapters', {}))} adapters, {len(valid_flows)} flows{skip})")
        for err in [*_validate_adapters(self.root, clean.get("adapters", {})), *dsl_errors]:
            _note(f"  {err}")
        self._analyzer = None
        return 0

    def step_repo_check(self, _args: argparse.Namespace) -> int:
        live = self.compose_index()
        issues = _validate_adapters(self.root, live.get("adapters", {}))
        _, _, dsl_errors = _partition_valid_contracts(self.root, live.get("units", {}), live.get("flows", {}))
        issues.extend(dsl_errors)

        idx_path = self.root / UnitsIndexer.INDEX_FILENAME
        if not idx_path.is_file():
            issues.append(f"[MISSING-INDEX] {idx_path.name} does not exist (run `sync`).")
        else:
            saved = json.loads(idx_path.read_text(encoding="utf-8"))
            if any(saved.get(k, {}) != live.get(k, {}) for k in ("units", "flows", "adapters")):
                issues.append("[STALE-INDEX] index.json is out of sync with current repository state (run `sync`).")
        for err in issues:
            _note(f"  {err}")
        return 1 if issues else 0

    def step_env_build(self, _args: argparse.Namespace) -> int:
        idx = self._last_index or self.compose_index()
        isolated = [m["path"] for m in idx.get("units", {}).values() if isinstance(m, dict) and m.get("isolated")]
        return self.loader.build_environments(isolated)

    def step_repo_fix(self, _args: argparse.Namespace) -> int:
        return self.loader.fix_repo()

    def step_repo_clean(self, _args: argparse.Namespace) -> int:
        return self.loader.clean_cache()

    def step_static_check(self, _args: argparse.Namespace) -> int:
        print(self.analyzer.evaluate_readiness())
        return 1 if self._had_dsl_errors else 0

    def step_trace(self, args: argparse.Namespace) -> int:
        res = [self.analyzer.run_trace(f, hybrid=args.hybrid) for f in self.analyzer.resolve_targets(args.target)]
        print(json.dumps(res[0] if len(res) == 1 else res, indent=2, ensure_ascii=False))
        return 0

    def step_inspect(self, args: argparse.Namespace) -> int:
        if not args.target:
            print(self.analyzer.format_list())
        else:
            for fname in self.analyzer.resolve_targets(args.target):
                print(self.analyzer.inspect_workflow(fname))
        return 0

    def step_emit_context(self, args: argparse.Namespace) -> int:
        matched = self.analyzer.resolve_targets(args.target) if self.analyzer.flow_names else [args.target.strip()]
        if len(matched) != 1:
            raise ValueError(f"'pack' requires a single flow, matched: {matched}")
        _, markdown, notes = collect_flow_markdown(self.root, matched[0], live=False)
        for note in notes:
            _note(f"[CTX] {note}")
        sys.stdout.write(markdown)
        return 0

    def step_arch_compass(self, args: argparse.Namespace) -> int:
        print(self.analyzer.evaluate_compass(run_suite=args.suite))
        return 0

    def run_pipeline(self, steps: tuple[str, ...], args: argparse.Namespace) -> int:
        for step in steps:
            if (rc := getattr(self, f"step_{step}")(args)) != 0:
                return rc
        return 1 if self._had_dsl_errors else 0

def build_parser() -> argparse.ArgumentParser:
    epilog = "\n".join(f"  {cmd:<10} {desc}" for cmd, (desc, _) in COMMANDS.items())
    p = argparse.ArgumentParser(prog="python -m fiber.studio.cli.reflex", epilog=f"commands:\n{epilog}", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", nargs="?", default="sync", choices=list(COMMANDS))
    p.add_argument("target", nargs="?", default="", help="Flow name/index or relative path (for 'new')")
    p.add_argument("--root", default="", help="Path to reflexor repository root")
    p.add_argument("--spec", default="", help="External blueprint JSON override")
    p.add_argument("--hybrid", action="store_true", help="Run real local py/exec units on 'verify'")
    p.add_argument("--suite", action="store_true", help="Run FSM verification suites on 'compass'")
    p.add_argument("--name", default="", help="@unit name override for 'new'")
    p.add_argument("--kind", default="auto", choices=["auto", "exec", "py", "worker", "adapter"])
    p.add_argument("--desc", default="TODO: description")
    p.add_argument("--force", action="store_true")
    return p

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in ("pack", "new") and not args.target:
        _note(f"[FLOW:ERR] '{args.cmd}' requires a target.")
        return 1
    is_init = args.cmd == "init"
    root_arg = args.root.strip() or ("reflexor" if is_init else None)
    try:
        session = reflexSession(root_arg, spec_arg=args.spec, allow_create=is_init)
        return session.run_pipeline(COMMANDS[args.cmd][1], args)
    except (ContextError, UnitLoaderError, OSError, KeyError, ValueError, RuntimeError) as exc:
        _note(f"[FLOW:ERR] {exc}")
        return 1

if __name__ == "__main__":
    sys.exit(main())