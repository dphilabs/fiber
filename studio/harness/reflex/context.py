# fiber.studio.harness.reflex.context
from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fiber.studio.runbook.unit.indexer import UnitsIndexer
from fiber.studio.runbook.unit.loader import UnitLoaderError, UnitsLoader
from xphi.arch.contract.registry.flow import FlowRegistry

_REPO_PKG_PREFIX = "reflexor."
_REPO_TOP_PACKAGES = ("units", "ops")
_FATAL_NOTE_PREFIXES = ("[UNRESOLVED]", "[MISSING-FILE]", "[READ-ERR]", "[SYNTAX-ERR]")
ENV_DIR = "REFLEXOR_DIR"


class ContextError(RuntimeError):
    """Raised when a flow context cannot be assembled."""


@dataclass(frozen=True)
class UnitRef:
    name: str
    scheme: str
    spec: str                   # raw unit DSL from the index
    desc: str
    path: str | None            # implementation file, None = external / contract only
    via: str | None = None      # unit that supplied `path` (mcp worker)


@dataclass
class SourceFile:
    path: str
    sha256: str
    text: str
    used_by: list[str] = field(default_factory=list)


@dataclass
class FlowContext:
    flow: str
    commit: str | None
    flows: list[tuple[str, str, str]]   # (name, spec, desc); root first, then sub-flows
    units: list[UnitRef]
    sources: list[SourceFile]
    notes: list[str]


def iter_repo_imports(tree: ast.AST, rel_path: str) -> Iterator[str]:
    """Dotted names of repo-internal modules (`units.*`, `ops.*`) imported by `rel_path`."""
    package = [p for p in rel_path.replace("\\", "/").split("/")[:-1] if p]
    seen: set[str] = set()

    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                up = node.level - 1
                if up > len(package):
                    continue  # relative import escapes repository root
                anchor = package[: len(package) - up]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            if base:
                candidates = [base, *(f"{base}.{a.name}" for a in node.names if a.name != "*")]
            elif node.level > 0:
                candidates = [a.name for a in node.names if a.name != "*"]
        else:
            continue

        for raw in candidates:
            name = raw.removeprefix(_REPO_PKG_PREFIX)
            top = name.split(".", 1)[0]
            if top in _REPO_TOP_PACKAGES and name not in seen:
                seen.add(name)
                yield name


class FlowContextCollector:
    def __init__(self, indexer: UnitsIndexer, *, live: bool = False) -> None:
        self.loader = indexer.loader
        if not (self.loader.local_repo_dir and self.loader.local_repo_dir.is_dir()):
            raise ContextError("A local reflexor checkout is required (set --root or REFLEXOR_DIR).")
        self.root = self.loader.local_repo_dir
        self.idx = indexer.load_catalog_index(force_refresh=live)
        self.reg = FlowRegistry().load_dict(self.idx)
        self.mcp_impl = {
            u.target: name
            for name, u in sorted(self.reg.units.items())
            if u.scheme == "mcp" and self._meta(name).get("path")
        }

    def _meta(self, name: str) -> dict[str, Any]:
        return self.idx.get("units", {}).get(name, {})

    def collect(self, flow_name: str) -> FlowContext:
        if flow_name not in self.reg.flows:
            raise ContextError(f"Unknown flow '{flow_name}'. Available: {', '.join(sorted(self.reg.flows))}")

        flows: list[str] = []
        unit_names: list[str] = []
        notes: list[str] = []
        self._walk(flow_name, set(), flows, unit_names, notes)

        units = [self._resolve(n) for n in unit_names]
        sources: dict[str, SourceFile] = {}
        for unit in units:
            if unit.path:
                self._expand(unit.path, unit.name, sources, notes)

        flow_meta = self.idx.get("flows", {})
        return FlowContext(
            flow=flow_name,
            commit=self.idx.get("commit"),
            flows=[(f, flow_meta[f]["flow"], flow_meta[f].get("desc", "")) for f in flows if f in flow_meta],
            units=units,
            sources=list(sources.values()),
            notes=notes,
        )

    def _walk(self, flow_name: str, seen: set[str], flows: list[str], units: list[str], notes: list[str]) -> None:
        """Step order traversal; gates skipped, heal targets and sub-flows included."""
        flows.append(flow_name)
        for step in self.reg.flows[flow_name].steps:
            if step.gate_expr is not None:
                continue
            for raw_name in filter(None, (step.name, step.heal_target)):
                # Strip inline bracket bindings (`unit[a=b]`) if present on raw step name
                name = raw_name.split("[", 1)[0].strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                if name in self.reg.flows:
                    self._walk(name, seen, flows, units, notes)
                elif name in self.reg.units:
                    units.append(name)
                    unit = self.reg.units[name]
                    if unit.scheme == "flow" and unit.target in self.reg.flows and unit.target not in seen:
                        seen.add(unit.target)
                        self._walk(unit.target, seen, flows, units, notes)
                else:
                    # Multi-entry fallback: `<unit_name>:<entry>` -> check base `<unit_name>` if needed
                    base_unit = name.split(":", 1)[0]
                    if base_unit in self.reg.units:
                        if base_unit not in units:
                            units.append(base_unit)
                    else:
                        notes.append(f"[UNRESOLVED] '{name}' (in flow '{flow_name}') is not a registered unit or flow.")

    def _resolve(self, name: str) -> UnitRef:
        """Index `path` is authoritative; contract targets may carry `ref@` prefixes or stale paths."""
        unit, meta = self.reg.units[name], self._meta(name)
        path, via = meta.get("path"), None
        if not path and ":" in name:
            path = self._meta(name.split(":", 1)[0]).get("path")
        if not path and unit.scheme == "mcp" and (via := self.mcp_impl.get(unit.target)):
            path = self._meta(via).get("path")
        elif not path and unit.scheme == "llm":
            path = unit.target.partition(":")[2].strip() or None
        return UnitRef(name, unit.scheme, meta.get("flow", ""), unit.desc, path, via)

    def _expand(self, entry: str, owner: str, sources: dict[str, SourceFile], notes: list[str]) -> None:
        stack: list[str] = [entry]
        visited_for_owner: set[str] = set()
        while stack:
            rel = stack.pop()
            if rel in visited_for_owner:
                continue
            visited_for_owner.add(rel)

            is_new = rel not in sources
            src = sources.get(rel) or self._load(rel, entry == rel, notes)
            if src is None:
                continue
            sources[rel] = src
            if owner not in src.used_by:
                src.used_by.append(owner)
            if rel.endswith(".py"):
                # Traverse transitive imports so `owner` is recorded in `used_by` across shared adapters
                deps = self._imports(src, notes) if is_new else self._cached_imports(src)
                stack.extend(reversed(deps))

    def _load(self, rel: str, is_entry: bool, notes: list[str]) -> SourceFile | None:
        if not (self.root / rel).is_file():
            notes.append(f"[MISSING-FILE] '{rel}'")
            return None
        try:
            data = self.loader.resolve_file(rel).read_bytes()
        except UnitLoaderError as exc:
            notes.append(f"[READ-ERR] {exc}")
            return None
        text = data.decode("utf-8", errors="replace")
        if not is_entry and not text.strip():
            return None  # empty `__init__.py` package marker
        return SourceFile(rel, hashlib.sha256(data).hexdigest(), text)

    def _imports(self, src: SourceFile, notes: list[str]) -> list[str]:
        try:
            tree = ast.parse(src.text, filename=src.path)
        except SyntaxError as exc:
            notes.append(f"[SYNTAX-ERR] '{src.path}:{exc.lineno}': {exc.msg}")
            return []
        return self._resolve_import_paths(tree, src.path)

    def _cached_imports(self, src: SourceFile) -> list[str]:
        try:
            tree = ast.parse(src.text, filename=src.path)
        except SyntaxError:
            return []
        return self._resolve_import_paths(tree, src.path)

    def _resolve_import_paths(self, tree: ast.AST, origin_path: str) -> list[str]:
        found: list[str] = []
        for module in iter_repo_imports(tree, origin_path):
            base = module.replace(".", "/")
            for candidate in (f"{base}.py", f"{base}/__init__.py"):
                if candidate != origin_path and candidate not in found and (self.root / candidate).is_file():
                    found.append(candidate)
                    break
        return found


def render_markdown(ctx: FlowContext) -> str:
    out = [
        f"# Flow Context: {ctx.flow}",
        "",
        f"- commit: `{ctx.commit or 'working tree'}`",
        f"- units: {len(ctx.units)} · sources: {len(ctx.sources)}",
        "",
    ]
    for name, spec, desc in ctx.flows:
        out += [f"## Flow `{name}`", "", *([f"> {desc}", ""] if desc else []), "```text", spec, "```", ""]

    out += ["## Units", ""]
    for i, u in enumerate(ctx.units, 1):
        where = f"`{u.path}`" + (f" (via `{u.via}`)" if u.via else "") if u.path else "external"
        out += [
            f"### {i}. {u.name} · {u.scheme} · {where}",
            "",
            *([f"> {u.desc}", ""] if u.desc else []),
            "```text",
            u.spec,
            "```",
            "",
        ]

    if ctx.notes:
        out += ["## Notes", "", *(f"- {n}" for n in ctx.notes), ""]

    out += ["## Sources", ""]
    for s in ctx.sources:
        out += [
            f'<file path="{s.path}" sha256="{s.sha256}" used_by="{", ".join(s.used_by)}">',
            s.text.rstrip("\n"),
            "</file>",
            "",
        ]
    return "\n".join(out)


def collect_flow_markdown(root: Path, flow_name: str, *, live: bool = False) -> tuple[bool, str, list[str]]:
    """Assembles flow context markdown and returns `(is_clean, markdown, notes)`."""
    indexer = UnitsIndexer(UnitsLoader(local_repo_dir=root.resolve()))
    ctx = FlowContextCollector(indexer, live=live).collect(flow_name)
    has_fatal = any(n.startswith(_FATAL_NOTE_PREFIXES) for n in ctx.notes)
    return (not has_fatal), render_markdown(ctx), ctx.notes


# --- read-only catalog (console runbook tabs) -------------------------------------


@dataclass
class Scenario:
    flow: str
    seeds: dict[str, dict[str, Any]]
    mock_outputs: dict[str, Any]
    file: str


class FlowCatalog:
    """Flows/units from the units index; seed + mock_outputs scanned from flow/**/*.json. Never writes back."""

    def __init__(self, root: Path | None, *, live: bool = False) -> None:
        self.root = root.resolve() if root is not None else None
        self.idx: dict[str, Any] = {}
        self.scenarios: dict[str, Scenario] = {}
        self.notes: list[str] = []
        if self.root is not None:
            self.reload(live=live)

    @classmethod
    def from_env(cls) -> FlowCatalog:
        raw = os.environ.get(ENV_DIR, "").strip()
        return cls(Path(raw).expanduser() if raw else None)

    @property
    def enabled(self) -> bool:
        return bool(self.idx.get("flows"))

    def reload(self, *, live: bool = False) -> None:
        self.idx, self.scenarios, self.notes = {}, {}, []
        if self.root is None:
            return
        if not (self.root / "flow").is_dir():
            self.notes.append("[MISSING-FILE] 'flow/'")
            return
        if not (self.root / "units").is_dir():
            self.notes.append("[MISSING-FILE] 'units/'")
            return
        try:
            self.idx = UnitsIndexer(UnitsLoader(local_repo_dir=self.root)).load_catalog_index(force_refresh=live)
        except (UnitLoaderError, OSError, ValueError) as exc:
            self.notes.append(f"[READ-ERR] index: {exc}")
        self._scan_scenarios()

    def _scan_scenarios(self) -> None:
        """Flow DSL definitions come from the index; scenario seeds/mock_outputs are scanned from `flow/**/*.json`."""
        assert self.root is not None
        for path in sorted((self.root / "flow").rglob("*.json")):
            rel = path.relative_to(self.root).as_posix()
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.notes.append(f"[READ-ERR] '{rel}': {exc}")
                continue
            if not isinstance(data, dict) or "units" in data or "flows" in data:
                continue
            if "seed" in data or "mock_outputs" in data:
                entries = {str(data.get("flow") or path.stem): data}
            else:
                entries = {
                    k: v
                    for k, v in data.items()
                    if isinstance(v, dict) and ("seed" in v or "mock_outputs" in v)
                }
            for flow, spec in entries.items():
                seeds = _seeds(spec.get("seed"))
                mocks = dict(spec.get("mock_outputs") or {})
                if existing := self.scenarios.get(flow):
                    existing.seeds.update(seeds)
                    existing.mock_outputs.update(mocks)
                else:
                    self.scenarios[flow] = Scenario(flow, seeds, mocks, rel)

    def flows(self) -> list[tuple[str, str, str]]:
        """(name, spec, desc), same shape as FlowContext.flows."""
        meta = self.idx.get("flows", {})
        return [(n, meta[n].get("flow", ""), meta[n].get("desc", "")) for n in sorted(meta)]

    def flow(self, name: str) -> tuple[str, str, str] | None:
        m = self.idx.get("flows", {}).get(name)
        return (name, m.get("flow", ""), m.get("desc", "")) if m else None

    def samples(self, flow: str) -> dict[str, dict[str, Any]]:
        sc = self.scenarios.get(flow)
        return dict(sc.seeds) if sc else {}

    def mock_outputs(self, flow: str) -> dict[str, Any]:
        sc = self.scenarios.get(flow)
        return dict(sc.mock_outputs) if sc else {}


def _seeds(seed: Any) -> dict[str, dict[str, Any]]:
    """list -> seed#n; dict of dicts -> named samples; plain dict -> one sample named 'seed'."""
    if isinstance(seed, list):
        return {f"seed#{i + 1}": s for i, s in enumerate(seed) if isinstance(s, dict)}
    if isinstance(seed, dict):
        if seed and all(isinstance(v, dict) for v in seed.values()):
            return dict(seed)
        return {"seed": seed}
    return {}