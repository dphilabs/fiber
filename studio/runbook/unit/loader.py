# fiber.studio.runbook.unit.loader
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
from typing import Any
import urllib.request

from xphi.arch.reflex.script import (
    has_uv,
    link_editable_package,
    run_unit_script,
    sync_script_env,
)

log = logging.getLogger("ops.units")

ENV_REPO_DIR = "REFLEXOR_DIR"
ENV_REMOTE_REPO = "FIBER_UNITS_REPO"

BASELINE_DIRS: tuple[str, ...] = (
    "units/adapters",
    "units/http",
    "units/fiber",
    "units/worker",
    "units/transform",
    "fixtures",
    "flow",
)
PACKAGE_ROOTS: tuple[str, ...] = ("units",)


class UnitLoaderError(RuntimeError):
    """Raised when a unit artifact cannot be fetched, verified, or executed."""


def resolve_repo_dir(
    explicit: str | Path | None = None,
    *,
    allow_create: bool = False,
) -> Path | None:
    """Single source of truth for resolving the local `reflexor` repository root."""
    if explicit is not None and str(explicit).strip():
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            if allow_create:
                root.mkdir(parents=True, exist_ok=True)
            else:
                raise NotADirectoryError(f"Repository root not found: {root}")
        return root

    if env_raw := os.environ.get(ENV_REPO_DIR, "").strip():
        root = Path(env_raw).expanduser().resolve()
        if root.is_dir():
            return root
        if allow_create:
            root.mkdir(parents=True, exist_ok=True)
            return root
        return None

    cwd_cand = (Path.cwd() / "reflexor").resolve()
    if (cwd_cand / "index.json").is_file() or (cwd_cand / "units").is_dir():
        return cwd_cand
    return None


def _safe_subpath(base: Path, rel_path: str) -> Path:
    base_resolved = base.resolve()
    candidate = (base_resolved / rel_path).resolve()
    if candidate != base_resolved and not candidate.is_relative_to(base_resolved):
        raise UnitLoaderError(f"Path '{rel_path}' escapes root '{base_resolved}'.")
    return candidate


class UnitsLoader:
    """Manages local/remote unit repositories, SHA256 verification, scaffolding, and execution."""

    def __init__(
        self,
        repo: str = "",
        default_ref: str = "main",
        cache_dir: str | Path | None = None,
        local_repo_dir: str | Path | None = None,
        *,
        template_dir: str | Path | None = None,
        allow_create: bool = False,
    ) -> None:
        self.repo = (repo or os.environ.get(ENV_REMOTE_REPO, "reflexor")).strip()
        self.default_ref = default_ref
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "reflexor").expanduser().resolve()
        self.template_dir = Path(template_dir).expanduser().resolve() if template_dir else None
        self._hash_lock: dict[str, str] = {}
        self.local_repo_dir: Path | None = resolve_repo_dir(local_repo_dir, allow_create=allow_create)

    @property
    def root(self) -> Path:
        if not self.local_repo_dir:
            raise UnitLoaderError("Local repository directory is not configured (set REFLEXOR_DIR).")
        return self.local_repo_dir

    def update_hash_locks(self, locks: dict[str, str]) -> None:
        self._hash_lock.update(locks)

    # Artifact Fetch & Resolution
    @staticmethod
    def _parse_ref_and_path(raw_target: str, default_ref: str) -> tuple[str, str]:
        if "@" in raw_target and not raw_target.startswith("@"):
            ref, _, rel_path = raw_target.partition("@")
            return ref.strip(), rel_path.strip().lstrip("/")
        return default_ref, raw_target.strip().lstrip("/")

    def fetch_raw_file(self, ref: str, rel_path: str) -> bytes:
        if "/" not in self.repo:
            raise UnitLoaderError(
                f"Local file '{rel_path}' not found in '{self.local_repo_dir}', and remote repo "
                f"'{self.repo}' is not in '<owner>/<repo>' format."
            )
        api_endpoint = f"repos/{self.repo}/contents/{rel_path}?ref={ref}"
        token = os.environ.get("FIBER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")

        if token:
            req = urllib.request.Request(
                f"https://api.github.com/{api_endpoint}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.raw+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.read()
            except Exception as exc:
                raise UnitLoaderError(f"Failed to fetch '{self.repo}/{rel_path}@{ref}' via token: {exc}") from exc

        if shutil.which("gh"):
            proc = subprocess.run(
                ["gh", "api", "-H", "Accept: application/vnd.github.raw+json", api_endpoint],
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                return proc.stdout
            err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
            raise UnitLoaderError(f"`gh api` failed for '{self.repo}/{rel_path}@{ref}': {err_msg}")

        raise UnitLoaderError(
            f"Cannot access private repo '{self.repo}': set GITHUB_TOKEN or authenticate with `gh auth login`."
        )

    @staticmethod
    def _verify_hash(data: bytes, expected_sha256: str | None, label: str) -> None:
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256.lower():
            raise UnitLoaderError(f"SHA256 integrity failure for '{label}'.")

    def resolve_file(self, target_expr: str, expected_sha256: str | None = None) -> Path:
        ref, rel_path = self._parse_ref_and_path(target_expr, self.default_ref)

        if self.local_repo_dir:
            local_candidate = _safe_subpath(self.local_repo_dir, rel_path)
            if local_candidate.is_file():
                if expected_sha256:
                    self._verify_hash(local_candidate.read_bytes(), expected_sha256, rel_path)
                return local_candidate

        locked_sha = expected_sha256 or self._hash_lock.get(rel_path)
        cached_file = _safe_subpath(self.cache_dir / ref, rel_path)
        if cached_file.is_file():
            try:
                self._verify_hash(cached_file.read_bytes(), locked_sha, rel_path)
                return cached_file
            except UnitLoaderError:
                cached_file.unlink(missing_ok=True)

        raw_bytes = self.fetch_raw_file(ref, rel_path)
        self._verify_hash(raw_bytes, locked_sha, rel_path)
        cached_file.parent.mkdir(parents=True, exist_ok=True)
        cached_file.write_bytes(raw_bytes)
        return cached_file

    # Execution Dispatch (`py` / `exec` / `pass`)
    def run_py(self, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        mod_part, _, fn_name = target.rpartition(":")
        if not mod_part or not fn_name:
            raise UnitLoaderError(f"Invalid py target '{target}', expected '<module_or_path>:<callable>'.")

        if not mod_part.endswith(".py") and "/" not in mod_part:
            out = getattr(importlib.import_module(mod_part), fn_name)(payload)
            return out if isinstance(out, dict) else None

        script_path = self.resolve_file(mod_part)
        extra_path = self.local_repo_dir.parent if self.local_repo_dir else None
        return run_unit_script(script_path, fn_name, payload, extra_sys_path=extra_path)

    def run_exec(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        tokens = shlex.split(target)
        if not tokens:
            raise UnitLoaderError("Empty exec target.")

        cmd_head = tokens[0]
        if "/" in cmd_head and not cmd_head.startswith("/"):
            resolved_bin = self.resolve_file(cmd_head)
            resolved_bin.chmod(resolved_bin.stat().st_mode | stat.S_IXUSR)
            tokens[0] = str(resolved_bin)

        payload_json = json.dumps(payload)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "FIBER_PAYLOAD_JSON": payload_json,
        }
        proc = subprocess.run(
            tokens,
            input=payload_json,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            err_detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise UnitLoaderError(f"Exec target '{target}' failed: {err_detail}")

        stdout = proc.stdout.strip()
        try:
            if isinstance(parsed := json.loads(stdout), dict):
                return parsed
        except ValueError:
            pass
        return {"stdout": stdout, "_score": payload.get("_score", 1.0)}

    def dispatch(self, scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if scheme == "pass":
            return dict(payload)
        if scheme == "py":
            return self.run_py(target, payload)
        if scheme == "exec":
            return self.run_exec(target, payload)
        raise UnitLoaderError(f"Unsupported scheme '{scheme}://' in UnitsLoader.")

    # Local Workspace & Repository Operations
    def ensure_layout(self) -> int:
        created = 0
        for rel in BASELINE_DIRS:
            target = self.root / rel
            if not target.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                created += 1
        log.info(f"[REPO] Layout verified at {self.root} ({created} directory created).")
        return 0

    def git_head_sha(self) -> str:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True, text=True, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def fix_repo(self) -> int:
        fixed_count = 0
        for sh in sorted(self.root.rglob("*.sh")):
            raw = sh.read_bytes()
            if b"\r\n" in raw:
                sh.write_bytes(raw.replace(b"\r\n", b"\n"))
                fixed_count += 1
            mode = sh.stat().st_mode
            if not (mode & stat.S_IXUSR):
                sh.chmod(mode | stat.S_IXUSR)
                fixed_count += 1

        for pkg_name in PACKAGE_ROOTS:
            pkg_root = self.root / pkg_name
            if not pkg_root.is_dir():
                continue
            for d in [pkg_root, *[p for p in sorted(pkg_root.rglob("*")) if p.is_dir()]]:
                if d.name in ("__pycache__", "assets"):
                    continue
                init_file = d / "__init__.py"
                if (d == pkg_root or any(d.glob("*.py"))) and not init_file.exists():
                    init_file.write_text("", encoding="utf-8")
                    fixed_count += 1

        log.info(f"[FIX] Completed ({fixed_count} artifact(s) adjusted).")
        return 0

    fix_scripts = fix_repo

    def discover_adapters(self) -> dict[str, list[str]]:
        if not self.local_repo_dir or not (adapters_dir := self.local_repo_dir / "units" / "adapters").is_dir():
            return {}
        found: dict[str, list[str]] = {}
        for sub in sorted(adapters_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith(("_", ".")):
                continue
            mods = [
                p.relative_to(self.local_repo_dir).as_posix()
                for p in sorted(sub.rglob("*.py"))
                if p.name != "__init__.py"
            ]
            if mods:
                found[sub.name] = mods
        return found

    def scaffold(
        self,
        rel_path: str,
        *,
        unit_name: str = "",
        kind: str = "auto",
        desc: str = "TODO: unit description",
        overwrite: bool = False,
        template_dir: str | Path | None = None,
    ) -> Path:
        rel_clean = rel_path.strip().lstrip("/")
        is_adapter_target = kind == "adapter" or (
            kind == "auto"
            and (rel_clean.startswith(("adapters/", "units/adapters/")))
            and not rel_clean.endswith(".py")
        )
        if is_adapter_target:
            domain_rel = rel_clean.removeprefix("units/").removeprefix("adapters/").strip("/")
            if not domain_rel:
                raise UnitLoaderError("Adapter scaffold requires a domain name (e.g. 'units/adapters/slack').")
            adapters_base = _safe_subpath(self.root, "units/adapters")
            pkg_dir = _safe_subpath(adapters_base, domain_rel)
            if pkg_dir.exists() and any(pkg_dir.iterdir()) and not overwrite:
                raise FileExistsError(f"Adapter package directory is not empty: {pkg_dir}")
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (self.root / "units" / "__init__.py").touch(exist_ok=True)
            (adapters_base / "__init__.py").touch(exist_ok=True)
            (pkg_dir / "__init__.py").touch(exist_ok=True)
            mod_name = pkg_dir.name
            stub = pkg_dir / "client.py"
            if not stub.exists() or overwrite:
                stub.write_text(
                    f'# reflexor.units.adapters.{mod_name}.client\n"""{desc}"""\nfrom __future__ import annotations\n',
                    encoding="utf-8",
                )
            log.info(f"[NEW] Initialized adapter package 'reflexor.units.adapters.{mod_name}' -> {pkg_dir}")
            return pkg_dir

        target = _safe_subpath(self.root, rel_clean)
        if target.exists() and not overwrite:
            raise FileExistsError(f"Unit script already exists: {target}")

        t_dir = Path(template_dir).resolve() if template_dir else self.template_dir
        if not t_dir:
            raise UnitLoaderError("`template_dir` is required to scaffold unit scripts.")

        resolved_name = unit_name.strip() or target.stem
        tmpl_name = (
            "worker.py.tmpl"
            if kind == "worker"
            else ("exec.sh.tmpl" if (kind == "exec" or target.suffix == ".sh") else "py.py.tmpl")
        )
        tmpl_path = t_dir / tmpl_name
        if not tmpl_path.is_file():
            raise FileNotFoundError(f"Unit template not found: {tmpl_path}")

        rendered = tmpl_path.read_text(encoding="utf-8").format(unit_name=resolved_name, rel_path=rel_clean, desc=desc)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered.replace("\r\n", "\n"), encoding="utf-8")
        if target.suffix == ".sh":
            target.chmod(target.stat().st_mode | stat.S_IXUSR)

        log.info(f"[NEW] Created unit '{resolved_name}' ({tmpl_name}) -> {target}")
        return target

    def build_environments(self, isolated_rel_paths: list[str]) -> int:
        mode, msg = link_editable_package(self.root)
        if mode == "editable":
            log.info(f"[BUILD] Linked editable package `{self.root.name}` -> {msg}")
        elif mode == "pth":
            log.info(f"[BUILD] Linked `{self.root.name}` via site-packages .pth -> {msg}")
        elif mode == "warn":
            log.info(f"[WARN] Editable link skipped/failed: {msg}")

        if not isolated_rel_paths:
            log.info("[BUILD] No isolated PEP 723 units found to pre-warm.")
            return 0
        if not has_uv():
            log.info("[WARN] `uv` executable not found; skipping isolated script pre-warm.")
            return 0

        log.info(f"[BUILD] Pre-warming {len(isolated_rel_paths)} isolated unit environment(s) via uv ...")
        for rel_path in sorted(isolated_rel_paths):
            try:
                sync_script_env(_safe_subpath(self.root, rel_path))
                log.info(f"  [WARM] {rel_path} -> OK")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                err_msg = exc.stderr.decode("utf-8", errors="replace").strip() if getattr(exc, "stderr", None) else str(exc)
                log.info(f"  [WARN] {rel_path} pre-warm failed: {err_msg}")

        log.info("[BUILD] OK — Package & unit environments ready.")
        return 0

    def clean_cache(self, cache_dir: Path | None = None) -> int:
        target = cache_dir or self.cache_dir
        shutil.rmtree(target, ignore_errors=True)
        log.info(f"[CLEAN] Removed local cache directory -> {target}")
        return 0