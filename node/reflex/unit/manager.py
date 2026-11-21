# fiber.node.reflex.unit.manager
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
import sys
from typing import Any

from fiber.node.reflex.unit.indexer import UnitsIndexer
from fiber.node.reflex.unit.loader import UnitsLoader
from xphi.arch.contract.reflex.script import PEP723_MARKER, has_pep723

log = logging.getLogger("ops.units")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_FORBIDDEN_ADAPTER_IMPORTS = ("reflexor",)

class UnitsManager:
    def __init__(
        self,
        repo_root: str | Path,
        *,
        template_dir: str | Path = TEMPLATE_DIR,
        allow_create: bool = False,
    ) -> None:
        self.loader = UnitsLoader(
            local_repo_dir=repo_root,
            template_dir=template_dir,
            allow_create=allow_create,
        )
        self.root = self.loader.root
        self.indexer = UnitsIndexer(self.loader)

    def _compose_index_payload(self) -> dict[str, Any]:
        data = self.indexer.build_repo_index(self.root)
        data["adapters"] = self.loader.discover_adapters()
        return data

    def _validate_adapters(self, adapters_map: dict[str, list[str]]) -> list[str]:
        """Validates `adapters/` namespace packages: syntax, no PEP 723 headers, no reverse `reflexor` imports."""
        errors: list[str] = []
        if not adapters_map:
            return errors

        pyproject = self.root / "pyproject.toml"
        if pyproject.is_file():
            if "adapters" not in pyproject.read_text(encoding="utf-8"):
                errors.append("[PKG-ADAPTER] `pyproject.toml` wheel include list does not cover `adapters/**`.")
        else:
            errors.append("[PKG-ADAPTER] Missing `pyproject.toml` at repo root (required to import `reflexor.units.adapters.*`).")

        for rel_files in adapters_map.values():
            for rel_py in rel_files:
                src = (self.root / rel_py).read_text(encoding="utf-8")

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

                for node in ast.walk(tree):
                    targets = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                    )
                    for mod_target in targets:
                        if mod_target.split(".", 1)[0] in _FORBIDDEN_ADAPTER_IMPORTS:
                            errors.append(
                                f"[ARCH-BOUNDARY] '{rel_py}' imports `{mod_target}` — "
                                "`reflexor/units/adapters` must not depend on `reflexor`."
                            )

        return errors

    def _check_repo(self, live_index: dict[str, Any], *, verify_saved_file: bool) -> list[str]:
        units_meta = live_index.get("units", {})
        flows_meta = live_index.get("flows", {})
        adapters_meta = live_index.get("adapters", {})
        issues = self._validate_adapters(adapters_meta)

        if verify_saved_file:
            idx_path = self.root / UnitsIndexer.INDEX_FILENAME
            if not idx_path.is_file():
                issues.append(f"[MISSING-INDEX] {idx_path.name} does not exist (run `sync`).")
            else:
                saved = json.loads(idx_path.read_text(encoding="utf-8"))
                if (
                    saved.get("units", {}) != units_meta
                    or saved.get("flows", {}) != flows_meta
                    or saved.get("adapters", {}) != adapters_meta
                ):
                    issues.append("[STALE-INDEX] index.json is out of sync with current repository state (run `sync`).")

        return issues

    def init(self) -> int:
        self.loader.ensure_layout()
        self.loader.fix_repo()
        return self.gen()

    def new(
        self,
        rel_path: str,
        *,
        unit_name: str = "",
        kind: str = "auto",
        desc: str = "TODO: unit description",
        overwrite: bool = False,
    ) -> int:
        self.loader.scaffold(rel_path, unit_name=unit_name, kind=kind, desc=desc, overwrite=overwrite)
        return self.gen()

    def check(self) -> int:
        data = self._compose_index_payload()
        issues = self._check_repo(data, verify_saved_file=True)
        for e in issues:
            log.info(f"  {e}")
        return 1 if issues else 0

    def fix(self) -> int:
        return self.loader.fix_repo()

    def gen(self) -> int:
        data = self._compose_index_payload()
        if head_sha := self.loader.git_head_sha():
            data["commit"] = head_sha

        idx_path = self.root / UnitsIndexer.INDEX_FILENAME
        idx_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log.info(
            f"[GEN] Wrote catalog index -> {idx_path} "
            f"({len(data.get('units', {}))} units, {len(data.get('adapters', {}))} adapters, {len(data.get('flows', {}))} flows)"
        )
        for warn in self._check_repo(data, verify_saved_file=False):
            log.info(f"  {warn}")
        return 0

    def build(self) -> int:
        self.gen()
        idx_path = self.root / UnitsIndexer.INDEX_FILENAME
        idx_data = json.loads(idx_path.read_text(encoding="utf-8"))
        isolated_units = [m["path"] for m in idx_data.get("units", {}).values() if isinstance(m, dict) and m.get("isolated")]
        return self.loader.build_environments(isolated_units)

    def clean(self) -> int:
        return self.loader.clean_cache()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m reflexor.flow.units.manager")
    parser.add_argument("cmd", nargs="?", default="gen", choices=["gen", "check", "build", "fix", "init", "clean"])
    parser.add_argument("--root", default="reflexor")
    args = parser.parse_args(argv)

    mgr = UnitsManager(args.root, allow_create=(args.cmd == "init"))
    return getattr(mgr, args.cmd)()

if __name__ == "__main__":
    sys.exit(main())