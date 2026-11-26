# fiber.node.bound.verify.deps
from __future__ import annotations

import ast
import importlib.util
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

@dataclass(frozen=True)
class DepsReport:
    declared: bool = False
    missing_req: List[str] = field(default_factory=list)
    missing_opt: List[str] = field(default_factory=list)
    source: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return not self.missing_req


class WorkerDepsChecker:
    """워커 상단 `__deps__` 리터럴을 AST로 읽어 마운트 가능 여부 및 선언 일관성을 검증"""
    INTERNAL_PREFIXES = ("xphi", "fiber", "theoria", "eco")

    @classmethod
    def filter_mountable_specs(cls, specs: Iterable[Any], logger: Any = None, daemon_name: str = "McpWorkerDaemon") -> Dict[str, Any]:
        """ResolvedTarget 목록에서 필수 의존성이 충족된 타겟만 추려 반환하고, 누락 내역을 로깅"""
        mountable: Dict[str, Any] = {}
        for spec in specs:
            report = cls.check_spec(spec)
            if not report.ok:
                if logger:
                    logger.warning(
                        f"[{daemon_name}] Skipping [{spec.target_id}] - missing required deps: {', '.join(report.missing_req)}"
                    )
                continue
            if report.missing_opt and logger:
                logger.info(
                    f"[{daemon_name}] [{spec.target_id}] degraded - missing optional deps: {', '.join(report.missing_opt)}"
                )
            mountable[spec.target_id] = spec
        return mountable

    @classmethod
    def check_spec(cls, spec: Any) -> DepsReport:
        """단일 ResolvedTarget(또는 exec_cmd 문자열)의 의존성 충족 여부를 판정"""
        exec_cmd = spec if isinstance(spec, str) else getattr(spec, "exec_cmd", "")
        env = None if isinstance(spec, str) else getattr(spec, "env_dict", None)

        py_file = cls._resolve_source(exec_cmd)
        if py_file is None:
            return DepsReport(declared=False)
        return cls.check_file(py_file, env=env)

    @classmethod
    def check_file(cls, py_file: Path, env: Optional[Dict[str, str]] = None) -> DepsReport:
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            deps = cls._read_deps_literal(tree)
        except Exception:
            return DepsReport(declared=False, source=py_file)

        if deps is None:
            return DepsReport(declared=False, source=py_file)

        path_var = (env or {}).get("PATH") or os.environ.get("PATH")
        missing_req = [f"py:{m}" for m in deps.get("py", []) if not cls._has_py(m)]
        missing_req += [f"bin:{b}" for b in deps.get("bins", []) if not cls._has_bin(b, path_var)]

        missing_opt = [f"py:{m}" for m in deps.get("optional_py", []) if not cls._has_py(m)]
        missing_opt += [f"bin:{b}" for b in deps.get("optional_bins", []) if not cls._has_bin(b, path_var)]

        return DepsReport(declared=True, missing_req=missing_req, missing_opt=missing_opt, source=py_file)

    @classmethod
    def audit_file(cls, py_file: Path) -> List[str]:
        """워커 파일의 실제 import / shutil.which 호출과 `__deps__` 선언 간 누락·불일치를 반환"""
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        deps = cls._read_deps_literal(tree) or {}
        req_py = {m.strip().split(".")[0] for expr in deps.get("py", []) for m in expr.split("|") if m.strip()}
        opt_py = {m.strip().split(".")[0] for expr in deps.get("optional_py", []) for m in expr.split("|") if m.strip()}
        all_bins = {
            Path(b.strip()).name
            for key in ("bins", "optional_bins")
            for expr in deps.get(key, [])
            for b in expr.split("|") if b.strip()
        }

        ignore_py: Set[str] = set(sys.stdlib_module_names) | set(cls.INTERNAL_PREFIXES) | {"__future__"}
        top_imports: Set[str] = set()
        guarded_imports: Set[str] = set()
        used_bins: Set[str] = set()

        for node in tree.body:
            top_imports.update(cls._iter_imported_roots(node, ignore_py))

        for node in ast.walk(tree):
            for name in cls._iter_imported_roots(node, ignore_py):
                if name not in top_imports:
                    guarded_imports.add(name)
            if isinstance(node, ast.Call):
                fn = node.func
                is_which = (isinstance(fn, ast.Attribute) and fn.attr == "which") or (
                    isinstance(fn, ast.Name) and fn.id == "which"
                )
                if is_which and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    used_bins.add(Path(node.args[0].value).name)

        issues: List[str] = []
        for mod in sorted(top_imports - req_py):
            note = " (in optional_py, but imported unconditionally at top-level)" if mod in opt_py else ""
            issues.append(f"Undeclared required import 'py:{mod}'{note}")
        for mod in sorted(guarded_imports - (req_py | opt_py)):
            issues.append(f"Undeclared optional import 'py:{mod}'")
        for b in sorted(used_bins - all_bins):
            issues.append(f"Undeclared binary 'bin:{b}' in shutil.which()")
        return issues

    @staticmethod
    def _has_py(expr: str) -> bool:
        return any(importlib.util.find_spec(m.strip()) is not None for m in expr.split("|") if m.strip())

    @staticmethod
    def _has_bin(expr: str, path: Optional[str]) -> bool:
        for item in (x.strip() for x in expr.split("|") if x.strip()):
            if "/" in item:
                if os.path.isfile(item) and os.access(item, os.X_OK):
                    return True
            elif shutil.which(item, path=path) is not None:
                return True
        return False

    @staticmethod
    def _resolve_source(exec_cmd: str) -> Optional[Path]:
        if not exec_cmd or exec_cmd.startswith(("http://", "https://")):
            return None
        try:
            tokens = shlex.split(exec_cmd)
        except ValueError:
            return None

        for i, tok in enumerate(tokens):
            if tok == "-m" and i + 1 < len(tokens):
                try:
                    spec = importlib.util.find_spec(tokens[i + 1])
                    return Path(spec.origin) if spec and spec.origin and spec.origin.endswith(".py") else None
                except (ImportError, ValueError):
                    return None
            if tok.endswith(".py") and Path(tok).is_file():
                return Path(tok).resolve()
        return None

    @staticmethod
    def _read_deps_literal(tree: ast.Module) -> Optional[Dict[str, List[str]]]:
        for node in tree.body:
            val_node = None
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__deps__" for t in node.targets):
                val_node = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "__deps__":
                val_node = node.value
            if val_node is not None:
                parsed = ast.literal_eval(val_node)
                return parsed if isinstance(parsed, dict) else {}
        return None

    @staticmethod
    def _iter_imported_roots(node: ast.AST, ignore: Set[str]) -> List[str]:
        if isinstance(node, ast.Import):
            roots = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots = [node.module.split(".")[0]]
        else:
            return []
        return [r for r in roots if r and r not in ignore]