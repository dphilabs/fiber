# fiber.studio.reflex.flow.catalog
from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any

from xphi.arch.contract.registry.flow import FlowRegistry

from fiber.node.reflex.runbook.inspector import emit_flow_dsl, emit_unit_dsl
from fiber.studio.reflex.indexer import UnitsIndexer
from fiber.studio.reflex.loader import UnitLoaderError, UnitsLoader, resolve_repo_dir

_REPO_PKG_PREFIX = "reflexor."
_REPO_TOP_PACKAGES = ("units", "flows", "fixtures", "ops")
_FATAL_NOTE_PREFIXES = ("[UNRESOLVED]", "[MISSING-FILE]", "[READ-ERR]", "[SYNTAX-ERR]")

class ContextError(RuntimeError):
    """Raised when a flow context cannot be assembled."""

@dataclass(frozen=True)
class UnitRef:
    name: str
    scheme: str
    spec: str
    desc: str
    path: str | None
    via: str | None = None


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
    flows: list[tuple[str, str, str]]
    units: list[UnitRef]
    sources: list[SourceFile]
    notes: list[str]


@dataclass
class Scenario:
    flow: str
    seeds: dict[str, dict[str, Any]]
    mock_outputs: dict[str, Any]
    file: str


def iter_repo_imports(tree: ast.AST, rel_path: str) -> Iterator[str]:
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
                    continue
                anchor = package[: len(package) - up]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            if base:
                candidates = [base, *(f"{base}.{a.name}" for a in node.names if a.name != "*")]
            elif node.level > 0:
                candidates = [a.name for a in node.names if a.name != "*"]
        for raw in candidates:
            name = raw.removeprefix(_REPO_PKG_PREFIX)
            if name.split(".", 1)[0] in _REPO_TOP_PACKAGES and name not in seen:
                seen.add(name)
                yield name


class FlowContextCollector:
    def __init__(self, indexer: UnitsIndexer, *, live: bool = False) -> None:
        self.loader = indexer.loader
        if not (self.loader.local_repo_dir and self.loader.local_repo_dir.is_dir()):
            raise ContextError("A local reflexor checkout is required (set --root or REFLEXOR_DIR).")
        self.root = self.loader.local_repo_dir
        self.idx = indexer.load_catalog_index(force_refresh=live)
        self.reg = FlowRegistry()
        indexer.sync_into_registry(self.reg, force_refresh=False)
        self.mcp_impl = {
            u.target: name for name, u in sorted(self.reg.units.items()) if u.scheme == "mcp" and self._path(name)
        }

    def _path(self, name: str) -> str | None:
        units = self.idx.get("units", {})
        meta = units.get(name) or (units.get(name.split(":", 1)[0]) if ":" in name else None)
        return meta.get("path") if isinstance(meta, dict) else None

    def collect(self, flow_name: str) -> FlowContext:
        if flow_name not in self.reg.flows:
            raise ContextError(f"Unknown flow '{flow_name}'. Available: {', '.join(sorted(self.reg.flows))}")

        flows, unit_names, notes = [], [], []
        self._walk(flow_name, set(), flows, unit_names, notes)
        units = [self._resolve(n) for n in unit_names]
        sources: dict[str, SourceFile] = {}
        for u in units:
            if u.path:
                self._expand(u.path, u.name, sources, notes)

        return FlowContext(
            flow=flow_name,
            commit=self.idx.get("commit") or self.loader.git_head_sha() or None,
            flows=[(f, emit_flow_dsl(self.reg.flows[f]), self.reg.flows[f].desc) for f in flows if f in self.reg.flows],
            units=units,
            sources=list(sources.values()),
            notes=notes,
        )

    def _walk(self, flow_name: str, seen: set[str], flows: list[str], units: list[str], notes: list[str]) -> None:
        flows.append(flow_name)
        for step in self.reg.flows[flow_name].steps:
            if step.gate_expr is not None:
                continue
            for raw_name in filter(None, (step.name, step.heal_target)):
                name = raw_name.split("[", 1)[0].strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                if name in self.reg.flows:
                    self._walk(name, seen, flows, units, notes)
                elif name in self.reg.units:
                    units.append(name)
                    u = self.reg.units[name]
                    if u.scheme == "flow" and u.target in self.reg.flows and u.target not in seen:
                        seen.add(u.target)
                        self._walk(u.target, seen, flows, units, notes)
                elif (base := name.split(":", 1)[0]) in self.reg.units:
                    if base not in units:
                        units.append(base)
                else:
                    notes.append(f"[UNRESOLVED] '{name}' (in flow '{flow_name}') is not a registered unit or flow.")

    def _resolve(self, name: str) -> UnitRef:
        unit = self.reg.units[name]
        path, via = self._path(name), None
        if not path and unit.scheme == "mcp" and (via := self.mcp_impl.get(unit.target)):
            path = self._path(via)
        elif not path and unit.scheme == "llm":
            cand = unit.target.partition(":")[2].strip()
            path = cand if cand and (self.root / cand).is_file() else None
        return UnitRef(name, unit.scheme, emit_unit_dsl(unit), unit.desc, path, via)

    def _expand(self, entry: str, owner: str, sources: dict[str, SourceFile], notes: list[str]) -> None:
        stack, visited = [entry], set()
        while stack:
            if (rel := stack.pop()) in visited:
                continue
            visited.add(rel)
            is_new = rel not in sources
            src = sources.get(rel) or self._load(rel, entry == rel, notes)
            if src is None:
                continue
            sources[rel] = src
            if owner not in src.used_by:
                src.used_by.append(owner)
            if rel.endswith(".py"):
                stack.extend(reversed(self._imports(src, notes if is_new else None)))

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
        return None if (not is_entry and not text.strip()) else SourceFile(rel, hashlib.sha256(data).hexdigest(), text)

    def _imports(self, src: SourceFile, notes: list[str] | None) -> list[str]:
        try:
            tree = ast.parse(src.text, filename=src.path)
        except SyntaxError as exc:
            if notes is not None:
                notes.append(f"[SYNTAX-ERR] '{src.path}:{exc.lineno}': {exc.msg}")
            return []
        found: list[str] = []
        for mod in iter_repo_imports(tree, src.path):
            base = mod.replace(".", "/")
            for cand in (f"{base}.py", f"{base}/__init__.py"):
                if cand != src.path and cand not in found and (self.root / cand).is_file():
                    found.append(cand)
                    break
        return found


def render_markdown(ctx: FlowContext) -> str:
    out = [f"# Flow Context: {ctx.flow}", "", f"- commit: `{ctx.commit or 'working tree'}`", f"- units: {len(ctx.units)} · sources: {len(ctx.sources)}", ""]
    for name, spec, desc in ctx.flows:
        out += [f"## Flow `{name}`", "", *([f"> {desc}", ""] if desc else []), "```text", spec, "```", ""]
    out += ["## Units", ""]
    for i, u in enumerate(ctx.units, 1):
        where = f"`{u.path}`" + (f" (via `{u.via}`)" if u.via else "") if u.path else "external"
        out += [f"### {i}. {u.name} · {u.scheme} · {where}", "", *([f"> {u.desc}", ""] if u.desc else []), "```text", u.spec, "```", ""]
    if ctx.notes:
        out += ["## Notes", "", *(f"- {n}" for n in ctx.notes), ""]
    out += ["## Sources", ""]
    for s in ctx.sources:
        out += [f'<file path="{s.path}" sha256="{s.sha256}" used_by="{", ".join(s.used_by)}">', s.text.rstrip("\n"), "</file>", ""]
    return "\n".join(out)


def collect_flow_markdown(root: Path, flow_name: str, *, live: bool = False) -> tuple[bool, str, list[str]]:
    indexer = UnitsIndexer(UnitsLoader(local_repo_dir=root.resolve()))
    ctx = FlowContextCollector(indexer, live=live).collect(flow_name)
    return not any(n.startswith(_FATAL_NOTE_PREFIXES) for n in ctx.notes), render_markdown(ctx), ctx.notes


def _seeds(seed: Any) -> dict[str, dict[str, Any]]:
    if isinstance(seed, list):
        return {f"seed#{i + 1}": s for i, s in enumerate(seed) if isinstance(s, dict)}
    if isinstance(seed, dict):
        return dict(seed) if (seed and all(isinstance(v, dict) for v in seed.values())) else {"seed": seed}
    return {}


class FlowCatalog:
    def __init__(self, root: Path | None, *, live: bool = False) -> None:
        self.root = root.resolve() if root is not None else None
        self.idx: dict[str, Any] = {}
        self.reg = FlowRegistry()
        self.scenarios: dict[str, Scenario] = {}
        self.notes: list[str] = []
        if self.root is not None:
            self.reload(live=live)

    @property
    def enabled(self) -> bool:
        return bool(self.reg.flows)

    @property
    def commit(self) -> str | None:
        if sha := self.idx.get("commit"):
            return str(sha)
        return UnitsLoader(local_repo_dir=self.root).git_head_sha() or None if self.root else None

    @classmethod
    def from_env(cls) -> FlowCatalog:
        return cls(resolve_repo_dir())

    @property
    def enabled(self) -> bool:
        return bool(self.reg.flows)

    def reload(self, *, live: bool = False) -> None:
        self.idx, self.scenarios, self.notes = {}, {}, []
        self.reg = FlowRegistry()
        if self.root is None:
            return
        for req in ("flows", "units"):
            if not (self.root / req).is_dir():
                self.notes.append(f"[MISSING-FILE] '{req}/'")
                return
        try:
            indexer = UnitsIndexer(UnitsLoader(local_repo_dir=self.root))
            self.idx = indexer.load_catalog_index(force_refresh=live)
            indexer.sync_into_registry(self.reg, force_refresh=False)
        except (UnitLoaderError, OSError, ValueError) as exc:
            self.notes.append(f"[READ-ERR] index: {exc}")
        self._scan_scenarios()

    def _scan_scenarios(self) -> None:
        assert self.root is not None
        for sub in ("flows", "fixtures"):
            base = self.root / sub
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.json")):
                rel = path.relative_to(self.root).as_posix()
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    self.notes.append(f"[READ-ERR] '{rel}': {exc}")
                    continue
                if not isinstance(data, dict):
                    continue
                if "flows" in data or "units" in data:
                    entries = {k: v for k, v in (data.get("flows") or {}).items() if isinstance(v, dict) and ("seed" in v or "mock_outputs" in v)}
                elif "seed" in data or "mock_outputs" in data:
                    entries = {str(data.get("flow") or path.stem): data}
                else:
                    entries = {k: v for k, v in data.items() if isinstance(v, dict) and ("seed" in v or "mock_outputs" in v)}
                for flow, spec in entries.items():
                    seeds, mocks = _seeds(spec.get("seed")), dict(spec.get("mock_outputs") or {})
                    if existing := self.scenarios.get(flow):
                        existing.seeds.update(seeds)
                        existing.mock_outputs.update(mocks)
                    elif seeds or mocks:
                        self.scenarios[flow] = Scenario(flow, seeds, mocks, rel)

    def flows(self) -> list[tuple[str, str, str]]:
        return [(n, emit_flow_dsl(fc), fc.desc) for n, fc in sorted(self.reg.flows.items())]

    def flow(self, name: str) -> tuple[str, str, str] | None:
        fc = self.reg.flows.get(name)
        return (name, emit_flow_dsl(fc), fc.desc) if fc else None

    def samples(self, flow: str) -> dict[str, dict[str, Any]]:
        return dict(sc.seeds) if (sc := self.scenarios.get(flow)) else {}

    def mock_outputs(self, flow: str) -> dict[str, Any]:
        return dict(sc.mock_outputs) if (sc := self.scenarios.get(flow)) else {}