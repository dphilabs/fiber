# fiber.node.reflex.unit.script
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
import tomllib
from typing import Any

PEP723_MARKER = "# /// script"


def has_uv() -> bool:
    return shutil.which("uv") is not None


def has_pep723(source_or_head: str) -> bool:
    return PEP723_MARKER in source_or_head[:1024]


def parse_pep723_deps(source_or_head: str) -> list[str]:
    lines: list[str] = []
    in_block = False
    for line in source_or_head.splitlines():
        stripped = line.strip()
        if stripped == PEP723_MARKER:
            in_block = True
        elif stripped == "# ///" and in_block:
            break
        elif in_block and line.startswith("#"):
            lines.append(line[1:].lstrip(" "))
    return list(tomllib.loads("\n".join(lines)).get("dependencies", [])) if lines else []


def link_editable_package(repo_root: Path) -> tuple[str, str]:
    """Editable 패키지 설치를 시도하고, 실패 시 `.pth` 링크로 폴백한다. (mode, message) 반환."""
    if not (repo_root / "pyproject.toml").is_file():
        return "skip", "No pyproject.toml found"

    if has_uv():
        proc = subprocess.run(
            ["uv", "pip", "install", "--no-deps", "-e", str(repo_root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return "editable", str(repo_root)
        err = proc.stderr.strip()
    else:
        err = "`uv` executable not found"

    candidates = [Path(p) for p in site.getsitepackages() if Path(p).is_dir()]
    if candidates:
        (candidates[0] / f"{repo_root.name}.pth").write_text(f"{repo_root.parent}\n", encoding="utf-8")
        if str(repo_root.parent) not in sys.path:
            sys.path.insert(0, str(repo_root.parent))
        return "pth", str(repo_root.parent)

    return "warn", err


def sync_script_env(script_path: str | Path, *, timeout: int = 120) -> None:
    """스크립트 본체(데몬 등)를 실행하지 않고 PEP 723 가상환경만 사전 동기화한다."""
    subprocess.run(
        ["uv", "sync", "--script", str(script_path)],
        check=True,
        capture_output=True,
        timeout=timeout,
    )


def exec_inprocess(
    script_path: Path,
    fn_name: str,
    payload: dict[str, Any],
    *,
    extra_sys_path: Path | None = None,
) -> dict[str, Any] | None:
    if extra_sys_path and str(extra_sys_path) not in sys.path:
        sys.path.insert(0, str(extra_sys_path))
    mod_id = f"_fiber_unit_{hashlib.sha1(str(script_path).encode()).hexdigest()[:10]}"
    spec = importlib.util.spec_from_file_location(mod_id, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Python spec from '{script_path}'.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_id] = module
    spec.loader.exec_module(module)
    out = getattr(module, fn_name)(payload)
    return out if isinstance(out, dict) else None


def exec_isolated_uv(
    script_path: Path,
    fn_name: str,
    payload: dict[str, Any],
    *,
    deps: list[str] | None = None,
    extra_pythonpath: Path | None = None,
    timeout: int = 120,
) -> dict[str, Any] | None:
    """`uv run --no-project`로 표준 라이브러리 shadowing(`http.py` 등) 없이 격리 실행한다."""
    resolved_deps = deps if deps is not None else parse_pep723_deps(script_path.read_text(encoding="utf-8")[:1024])
    with_args = [arg for d in resolved_deps for arg in ("--with", d)]
    runner_code = (
        "import json, sys, importlib.util; "
        f"spec = importlib.util.spec_from_file_location('_u', {str(script_path)!r}); "
        "m = importlib.util.module_from_spec(spec); sys.modules['_u'] = m; spec.loader.exec_module(m); "
        f"res = getattr(m, {fn_name!r})(json.loads(sys.stdin.read())); "
        "print(json.dumps(res or {}))"
    )
    env = os.environ.copy()
    if extra_pythonpath:
        prev_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{extra_pythonpath}:{prev_pp}".rstrip(":")

    proc = subprocess.run(
        ["uv", "run", "--no-project", *with_args, "python", "-c", runner_code],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return json.loads(proc.stdout.strip() or "{}")


def run_unit_script(
    script_path: Path,
    fn_name: str,
    payload: dict[str, Any],
    *,
    extra_sys_path: Path | None = None,
) -> dict[str, Any] | None:
    """인프로세스 실행을 우선 시도하고, 패키지 누락 시 PEP 723 `uv` 격리 실행으로 폴백한다."""
    try:
        return exec_inprocess(script_path, fn_name, payload, extra_sys_path=extra_sys_path)
    except ModuleNotFoundError:
        head = script_path.read_text(encoding="utf-8")[:1024]
        if not (has_pep723(head) and has_uv()):
            raise
        return exec_isolated_uv(
            script_path,
            fn_name,
            payload,
            deps=parse_pep723_deps(head),
            extra_pythonpath=extra_sys_path,
        )