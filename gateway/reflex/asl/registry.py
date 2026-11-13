# fiber.gateway.reflex.asl.registry
## @lineage: fiber.studio.base.fsm.registry
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xphi.node.space.bind.resolver import resolve_path

ASL_ROOT = Path(str(resolve_path("compats"))) / "asl"
BUILTIN_SCHEMA_FILES: Dict[str, str] = {
    "dataset_etl": "dataset_etl.json",
    "dataset_mapper": "dataset_mapper.json",
    "multiverse_ref": "multiverse_ref.json",
    "polyglot_dag": "polyglot_dag.json",
}


@dataclass(frozen=True)
class ASLEntry:
    name: str
    schema: Dict[str, Any]
    source_path: Optional[Path] = None

    def clone_schema(self) -> Dict[str, Any]:
        return copy.deepcopy(self.schema)


@dataclass
class ASLRegistry:
    search_dirs: List[Path] = field(default_factory=list)
    builtin_files: Dict[str, str] = field(default_factory=lambda: dict(BUILTIN_SCHEMA_FILES))
    _entries: Dict[str, ASLEntry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for d in [ASL_ROOT]:
            p = Path(d).expanduser().resolve()
            if p not in self.search_dirs:
                self.search_dirs.append(p)

    def add_dir(self, dir_path: str | Path) -> "ASLRegistry":
        p = Path(dir_path).expanduser().resolve()
        if p not in self.search_dirs:
            self.search_dirs.insert(0, p)
        return self

    def register(
        self,
        name: str,
        schema: Dict[str, Any],
        source_path: Optional[Path] = None,
    ) -> ASLEntry:
        key = self._normalize_key(name)
        entry = ASLEntry(name=key, schema=copy.deepcopy(schema), source_path=source_path)
        self._entries[key] = entry
        return entry

    def get(self, name_or_path: str) -> Optional[Tuple[Dict[str, Any], str]]:
        raw = name_or_path.strip()
        key = self._normalize_key(raw)

        if key in self._entries:
            entry = self._entries[key]
            src = entry.source_path.name if entry.source_path else f"registry:{key}"
            return entry.clone_schema(), src

        direct = Path(raw).expanduser()
        if direct.is_file():
            return self._load_and_cache(key, direct.resolve())

        target_filenames: List[str] = []
        if key in self.builtin_files:
            target_filenames.append(self.builtin_files[key])
        for cand_name in (f"{key}.json", f"{key}.asl.json", raw):
            if cand_name not in target_filenames:
                target_filenames.append(cand_name)

        for d in self.search_dirs:
            p_dir = Path(d)
            if not p_dir.is_dir():
                continue
            for fname in target_filenames:
                cand = p_dir / fname
                if cand.is_file():
                    return self._load_and_cache(key, cand.resolve())

        return None

    def _load_and_cache(self, key: str, resolved_path: Path) -> Tuple[Dict[str, Any], str]:
        schema = json.loads(resolved_path.read_text(encoding="utf-8"))
        self.register(key, schema, source_path=resolved_path)
        return copy.deepcopy(schema), resolved_path.name

    def list_available(self) -> List[str]:
        keys = set(self._entries.keys()) | set(self.builtin_files.keys())
        for d in self.search_dirs:
            p_dir = Path(d)
            if p_dir.is_dir():
                for f in p_dir.glob("*.json"):
                    stem = f.name[:-9] if f.name.endswith(".asl.json") else f.stem
                    keys.add(stem)
        return sorted(keys)

    @staticmethod
    def _normalize_key(raw: str) -> str:
        k = raw.strip()
        if k.startswith("asl://"):
            k = k[6:].strip("/")
        if k.endswith(".asl.json"):
            k = k[:-9]
        elif k.endswith(".json"):
            k = k[:-5]
        return k

default_asl_registry = ASLRegistry()