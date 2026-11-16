# fiber.node.reflex.unit.loader
## @lineage: fiber.reflex.unit.loader
from __future__ import annotations
import hashlib
import importlib
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

class UnitLoaderError(RuntimeError):
    """Raised when a unit artifact cannot be fetched, verified, or executed."""

class UnitsLoader:
    """Fetches, verifies (SHA256), and executes `py` and `exec` units from `dphilabs/fiber-units`."""
    def __init__(
        self,
        repo: str = "dphilabs/fiber-units",
        default_ref: str = "main",
        cache_dir: str | Path | None = None,
        local_repo_dir: str | Path | None = None,
    ) -> None:
        self.repo = repo
        self.default_ref = default_ref
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "fiber-units")
        env_local = os.environ.get("FIBER_UNITS_DIR")
        self.local_repo_dir = Path(local_repo_dir or env_local) if (local_repo_dir or env_local) else None
        self._hash_lock: dict[str, str] = {}

    def update_hash_locks(self, locks: dict[str, str]) -> None:
        """Receive relative_path -> sha256 mappings from `FiberUnitsIndexer`."""
        self._hash_lock.update(locks)

    @staticmethod
    def _parse_ref_and_path(raw_target: str, default_ref: str) -> tuple[str, str]:
        if "@" in raw_target and not raw_target.startswith("@"):
            ref, _, rel_path = raw_target.partition("@")
            return ref.strip(), rel_path.strip().lstrip("/")
        return default_ref, raw_target.strip().lstrip("/")

    def fetch_raw_file(self, ref: str, rel_path: str) -> bytes:
        """Fetch raw file bytes from private GitHub repo via GITHUB_TOKEN or `gh api` fallback."""
        api_endpoint = f"repos/{self.repo}/contents/{rel_path}?ref={ref}"
        token = os.environ.get("FIBER_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")

        if token:
            url = f"https://api.github.com/{api_endpoint}"
            req = urllib.request.Request(
                url,
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

    def resolve_file(self, target_expr: str, expected_sha256: str | None = None) -> Path:
        """Resolve a relative path in `fiber-units` to a local SHA256-verified file Path."""
        ref, rel_path = self._parse_ref_and_path(target_expr, self.default_ref)
        locked_sha = expected_sha256 or self._hash_lock.get(rel_path)

        if self.local_repo_dir and (local_candidate := self.local_repo_dir / rel_path).is_file():
            self._verify_hash(local_candidate.read_bytes(), locked_sha, rel_path)
            return local_candidate

        cached_file = self.cache_dir / ref / rel_path
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

    @staticmethod
    def _verify_hash(data: bytes, expected_sha256: str | None, label: str) -> None:
        if not expected_sha256:
            return
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256.lower():
            raise UnitLoaderError(f"SHA256 integrity failure for '{label}': expected {expected_sha256}, got {actual}")

    def run_py(self, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        mod_part, _, fn_name = target.rpartition(":")
        if not mod_part or not fn_name:
            raise UnitLoaderError(f"Invalid py target '{target}', expected '<module_or_path>:<callable>'.")

        if not mod_part.endswith(".py") and "/" not in mod_part:
            mod = importlib.import_module(mod_part)
            fn = getattr(mod, fn_name)
            out = fn(payload)
            return out if isinstance(out, dict) else None

        script_path = self.resolve_file(mod_part)
        source_head = script_path.read_text(encoding="utf-8")[:512]

        if "# /// script" in source_head and shutil.which("uv"):
            return self._run_pep723_with_uv(script_path, fn_name, payload)

        spec = importlib.util.spec_from_file_location(f"_fiber_unit_{script_path.stem}", script_path)
        if spec is None or spec.loader is None:
            raise UnitLoaderError(f"Cannot load Python spec from '{script_path}'.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, fn_name)
        out = fn(payload)
        return out if isinstance(out, dict) else None

    @staticmethod
    def _run_pep723_with_uv(script_path: Path, fn_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        runner_code = (
            "import json, sys, importlib.util; "
            f"spec = importlib.util.spec_from_file_location('_u', {str(script_path)!r}); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            f"res = getattr(m, {fn_name!r})(json.loads(sys.stdin.read())); "
            "print(json.dumps(res or {}))"
        )
        proc = subprocess.run(
            ["uv", "run", "--script", str(script_path), "python", "-c", runner_code],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout.strip() or "{}")

    def run_exec(self, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        tokens = shlex.split(target)
        if not tokens:
            raise UnitLoaderError("Empty exec target.")

        cmd_head = tokens[0]
        if "/" in cmd_head and not cmd_head.startswith("/"):
            resolved_bin = self.resolve_file(cmd_head)
            resolved_bin.chmod(resolved_bin.stat().st_mode | stat.S_IXUSR)
            tokens[0] = str(resolved_bin)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "FIBER_PAYLOAD_JSON": json.dumps(payload),
        }
        proc = subprocess.run(tokens, env=env, capture_output=True, text=True, check=True)
        stdout = proc.stdout.strip()
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
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