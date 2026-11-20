# fiber.studio.base.reflex.arch
## @lineage: theoria.reflex.trace.arch
## @lineage: reflexor.flow.trace.arch
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from fiber.node.reflex.workspace.viewport import WorkflowViewport
from fiber.node.reflex.workspace.inspector import emit_flow_dsl, emit_unit_dsl

REFLEX_LAYER_GLOBS: tuple[tuple[str, str, str], ...] = (
    ("L1: Pure Contract & AST Parser (xphi)", "xphi", "arch/contract/reflex/*.py"),
    ("L1: Pure Contract & AST Parser (xphi)", "xphi", "arch/contract/registry/flow.py"),
    ("L1: Pure Contract & AST Parser (xphi)", "xphi", "arch/xor/fsm/parser.py"),
    ("L2: WASM Compiler & Graph Watcher (xphi)", "xphi", "arch/xor/fsm/compiler.py"),
    ("L2: WASM Compiler & Graph Watcher (xphi)", "xphi", "watcher/reflex/**/*.py"),
    ("L2: WASM Compiler & Graph Watcher (xphi)", "xphi", "node/runtime/reflex/*.py"),
    ("L3: Polyglot Actuator Bridge (fiber)", "fiber", "gateway/bridge/fsm/*.py"),
    ("L4: Node Flow & Viewport Runtime (fiber)", "fiber", "node/reflex/**/*.py"),
    ("L5: Ops & Verification (theoria)", "theoria", "ops/reflex/**/*.py"),
)

TRACKED_IMPORT_KEYWORDS: tuple[str, ...] = (
    "reflex",
    "fsm",
    "registry.flow",
    "registry.asl",
    "runtime.wasm",
)


@dataclass
class ModuleMeta:
    layer: str
    mod_name: str
    rel_path: str
    lineages: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    reflex_imports: list[str] = field(default_factory=list)
    patch_targets: list[str] = field(default_factory=list)


def resolve_workspace_root() -> Path:
    """Locates the parent directory containing `xphi`, `fiber`, and `theoria`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "fiber").is_dir() and (parent / "xphi").is_dir():
            return parent
    return Path.cwd()


def classify_module_layer(mod_name: str) -> int:
    """Returns numeric layer rank (1..5) for any module path in the workspace."""
    if mod_name.startswith(("xphi.arch.contract.", "xphi.arch.xor.fsm.parser")):
        return 1
    if mod_name.startswith((
        "xphi.arch.xor.fsm.compiler",
        "xphi.watcher.reflex",
        "xphi.node.runtime",
        "xphi.state.inter.fsm",
        "xphi.arch.bound",
    )):
        return 2
    if mod_name.startswith("fiber.gateway.bridge.fsm"):
        return 3
    if mod_name.startswith("fiber.node.reflex"):
        return 4
    if mod_name.startswith(("theoria.ops.reflex", "ops.reflex")):
        return 5
    return 0


def parse_module_ast(layer: str, py_file: Path, ws_root: Path) -> ModuleMeta:
    rel_to_ws = py_file.relative_to(ws_root)
    mod_name = ".".join(rel_to_ws.with_suffix("").parts)
    src = py_file.read_text(encoding="utf-8", errors="replace")

    lineages = [
        line.split("@lineage:", 1)[1].strip()
        for line in src.splitlines()[:25]
        if "@lineage:" in line
    ]

    classes: list[str] = []
    functions: list[str] = []
    imports_seen: dict[str, list[str]] = {}
    patch_targets: list[str] = []

    try:
        tree = ast.parse(src)
        # 1. Top-level public symbols
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                functions.append(node.name)

        # 2. Full AST walk for all imports (including lazy imports) & mock.patch targets
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(k in node.module for k in TRACKED_IMPORT_KEYWORDS):
                    bucket = imports_seen.setdefault(node.module, [])
                    for alias in node.names:
                        if alias.name not in bucket:
                            bucket.append(alias.name)
            elif isinstance(node, ast.Call):
                func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if func_name == "patch" and node.args and isinstance(node.args[0], ast.Constant):
                    val = node.args[0].value
                    if isinstance(val, str) and "." in val:
                        patch_targets.append(val)
    except SyntaxError:
        pass

    reflex_imports = []
    for mod, names in imports_seen.items():
        preview = ", ".join(names[:4]) + (", ..." if len(names) > 4 else "")
        reflex_imports.append(f"{mod} ({preview})")

    return ModuleMeta(
        layer=layer,
        mod_name=mod_name,
        rel_path=str(rel_to_ws),
        lineages=lineages,
        classes=classes,
        functions=functions,
        reflex_imports=reflex_imports,
        patch_targets=patch_targets,
    )


def scan_reflex_architecture(ws_root: Path, keyword: str = "") -> list[ModuleMeta]:
    seen: set[Path] = set()
    results: list[ModuleMeta] = []

    for layer, pkg_name, pattern in REFLEX_LAYER_GLOBS:
        pkg_dir = ws_root / pkg_name
        if not pkg_dir.is_dir():
            continue
        for py_file in sorted(pkg_dir.glob(pattern)):
            if py_file.name == "__init__.py" or py_file in seen:
                continue
            seen.add(py_file)
            meta = parse_module_ast(layer, py_file, ws_root)
            if keyword:
                haystack = " ".join([meta.mod_name, *meta.lineages, *meta.classes, *meta.functions]).lower()
                if keyword.lower() not in haystack:
                    continue
            results.append(meta)
    return results


def print_arch_map(modules: Iterable[ModuleMeta]) -> None:
    current_layer = ""
    for m in modules:
        if m.layer != current_layer:
            current_layer = m.layer
            print(f"\n=== [{current_layer}] ===")
        lin_str = f"  (<- {', '.join(m.lineages[:2])})" if m.lineages else ""
        print(f"  * {m.mod_name}{lin_str}")
        if m.classes:
            print(f"      Classes   : {', '.join(m.classes)}")
        if m.functions:
            print(f"      Functions : {', '.join(m.functions[:8])}")


def print_dep_graph(modules: Iterable[ModuleMeta]) -> None:
    print("=== [Reflex Cross-Module Dependency Wiring (L1 -> L5)] ===")
    for m in modules:
        if not m.reflex_imports and not m.patch_targets:
            continue
        print(f"  * [{m.layer[:2]}] {m.mod_name}")
        for imp in m.reflex_imports:
            target_mod = imp.split(" (")[0].strip()
            t_rank = classify_module_layer(target_mod)
            tag = f"L{t_rank}" if t_rank else "EXT"
            print(f"      -> [{tag}] {imp}")
        for pt in m.patch_targets:
            print(f"      ~> [MOCK_PATCH] {pt}")


def print_scheme_breakdown() -> None:
    vp = WorkflowViewport(auto_sync_catalog=False)
    by_scheme: dict[str, list[str]] = {}
    for u in vp.registry.units.values():
        by_scheme.setdefault(u.scheme, []).append(f"{u.name}: {emit_unit_dsl(u)}")

    print("=== [Registered Units Grouped by Execution Scheme] ===")
    for scheme, items in sorted(by_scheme.items()):
        print(f"\n  [{scheme.upper()} Scheme ({len(items)})]")
        for item in items:
            print(f"    - {item}")

    print("\n=== [Registered Workflows & Sub-Flow Composition] ===")
    for fname, fobj in vp.registry.flows.items():
        print(f"    - {fname:<20}: {emit_flow_dsl(fobj)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ops.reflex.arch")
    parser.add_argument("mode", nargs="?", default="map", choices=["map", "deps", "schemes"])
    parser.add_argument("--root", default="", help="Workspace root containing xphi/fiber/theoria")
    parser.add_argument("--filter", default="", help="Filter modules by name, class, or @lineage")
    args = parser.parse_args()

    ws_root = Path(args.root).resolve() if args.root else resolve_workspace_root()

    if args.mode == "schemes":
        print_scheme_breakdown()
        return

    modules = scan_reflex_architecture(ws_root, keyword=args.filter)
    if args.mode == "map":
        print_arch_map(modules)
    elif args.mode == "deps":
        print_dep_graph(modules)

if __name__ == "__main__":
    main()