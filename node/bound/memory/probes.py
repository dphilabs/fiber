# fiber.node.bound.memory.probes
from __future__ import annotations

import ast
import asyncio
from collections.abc import Sequence
import copy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
from typing import Any, Protocol

from fiber.llm.compat.secret.redact import redact_string
from fiber.node.bound.memory.sandbox import TabularSandbox
from fiber.node.bound.memory.spec import RefKind, RefSource, RefSpec, RefStatus
from fiber.node.bound.reader.parsers import parse_document
from fiber.node.bound.reader.runtime import ReaderRuntimeOpts
from xphi.arch.contract.protocol.binder import ToolError

_GIT_REV_HEX_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_RESOLVE_ROW_CAP = 100
_GIT_TIMEOUT_SEC = 5.0
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def reject_if_secret(payload: Any, field_name: str) -> None:
    if isinstance(payload, str):
        if payload and redact_string(payload) != payload:
            raise ToolError(f"Secret or credential detected in '{field_name}'; storage refused.")
    elif isinstance(payload, dict):
        for k, v in payload.items():
            reject_if_secret(str(k), field_name)
            reject_if_secret(v, f"{field_name}.{k}")
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            reject_if_secret(item, f"{field_name}[{idx}]")


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    path: str
    status: RefStatus
    expected_hash: str
    hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "status": self.status.value,
            "expected_hash": self.expected_hash,
        }
        if self.hash is not None:
            out["hash"] = self.hash
        return out


@dataclass(frozen=True, slots=True)
class ResolvedRef:
    kind: RefKind
    status: RefStatus
    addr: str | None = None
    repo: str | None = None
    rev: str | None = None
    sql: str | None = None
    expected_hash: str | None = None
    hash: str | None = None
    heading: str | None = None
    loc: Any = None
    body: str | None = None
    sources: list[dict[str, Any]] | None = None
    row_count: int | None = None
    rows: list[dict[str, Any]] | None = None
    candidates: list[str] | None = None

    @staticmethod
    def status_of(expected: str, current: str) -> RefStatus:
        return RefStatus.OK if (not expected or current == expected) else RefStatus.STALE

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind.value, "status": self.status.value}
        for name in (
            "addr",
            "repo",
            "rev",
            "sql",
            "expected_hash",
            "hash",
            "heading",
            "loc",
            "body",
            "sources",
            "row_count",
            "rows",
        ):
            val = getattr(self, name)
            if val is not None:
                out[name] = val
        if self.candidates:
            out["candidates"] = self.candidates
        return out


class ProbeEnv:
    def __init__(self, root: Path, reader_opts: ReaderRuntimeOpts) -> None:
        self.root = root
        self.sandbox = TabularSandbox(root)
        self.reader_opts = reader_opts
        self._git_bin = self._find_binary("git")

    @staticmethod
    def sha12_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]

    @staticmethod
    def sha12_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:12]

    @staticmethod
    def validate_rev(rev: str) -> str:
        clean = rev.strip()
        if not _GIT_REV_HEX_RE.match(clean):
            raise ValueError(f"Invalid git revision hex format: {rev!r}")
        return clean.lower()

    @staticmethod
    def _find_binary(name: str) -> str | None:
        path_env = os.environ.get("PATH", "")
        aug = os.pathsep.join([path_env, *_EXTRA_BIN_DIRS]) if path_env else os.pathsep.join(_EXTRA_BIN_DIRS)
        return shutil.which(name, path=aug)

    def resolve_file(self, rel_path: str) -> tuple[Path | None, str]:
        clean = rel_path.strip()
        if not clean:
            raise ValueError("Reference path must not be empty.")
        cand = (self.root / clean).resolve()
        if not cand.is_relative_to(self.root):
            raise ValueError(f"Reference path outside workspace root is forbidden: {rel_path}")
        return (cand if cand.is_file() else None), cand.relative_to(self.root).as_posix()

    def resolve_dir(self, rel_dir: str) -> tuple[Path | None, str]:
        clean = (rel_dir or ".").strip()
        cand = (self.root / clean).resolve()
        if not cand.is_relative_to(self.root):
            raise ValueError(f"Repository path outside workspace root is forbidden: {rel_dir}")
        norm_rel = "." if cand == self.root else cand.relative_to(self.root).as_posix()
        return (cand if cand.is_dir() else None), norm_rel

    def split_fragment(self, raw_addr: str) -> tuple[Path | None, str, str, str]:
        file_part, _, frag = raw_addr.partition("#")
        target = frag.strip()
        fpath, norm_rel = self.resolve_file(file_part)
        return fpath, norm_rel, target, f"{norm_rel}#{target}"

    async def run_git(self, repo_dir: Path, *args: str, require_git: bool = True) -> tuple[int, str]:
        git_bin = self._git_bin or self._find_binary("git")
        if not git_bin:
            if require_git:
                raise ToolError("Required binary 'git' is not installed.")
            return 127, ""
        proc = await asyncio.create_subprocess_exec(
            git_bin,
            "-C",
            str(repo_dir),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ToolError(f"git command timed out after {_GIT_TIMEOUT_SEC}s.") from None
        return (proc.returncode or 0), out_b.decode("utf-8", "replace").strip()

    async def head_rev(self) -> str:
        rc, out = await self.run_git(
            self.root, "rev-parse", "--verify", "--quiet", "--end-of-options", "HEAD", require_git=False
        )
        return out[:12] if (rc == 0 and out) else ""

    async def git_rename_candidates(self, base_rev: str, missing_rel: str, sym_name: str) -> list[str]:
        if not base_rev or not _GIT_REV_HEX_RE.match(base_rev):
            return []
        rc, out = await self.run_git(
            self.root,
            "log",
            "--diff-filter=R",
            "-M",
            "--name-status",
            "--pretty=format:",
            f"{base_rev}..HEAD",
            require_git=False,
        )
        if rc != 0 or not out:
            return []
        cands: list[str] = []
        for line in out.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 3 and parts[0].startswith("R") and parts[1] == missing_rel:
                cand = f"{parts[2].strip()}#{sym_name}" if sym_name else parts[2].strip()
                if cand not in cands:
                    cands.append(cand)
        return cands


class RefProbe(Protocol):
    async def prepare(self, env: ProbeEnv, spec: RefSpec, default_rev: str) -> RefSpec: ...
    async def resolve(self, env: ProbeEnv, spec: RefSpec) -> ResolvedRef: ...


class SectionProbe:
    @staticmethod
    def _find_section(env: ProbeEnv, fpath: Path, sec_id: str) -> tuple[Any | None, str]:
        secs = parse_document(fpath, env.reader_opts)
        matched = next((s for s in secs if s.section_id == sec_id), None)
        if matched is None:
            return None, ""
        return matched, env.sha12_text(f"{matched.heading}\n{matched.body}")

    async def prepare(self, env: ProbeEnv, spec: RefSpec, _: str) -> RefSpec:
        fpath, norm_rel, sec_id, addr = env.split_fragment(spec.addr)
        if spec.hash:
            return RefSpec(kind=RefKind.SECTION, addr=addr, hash=spec.hash)
        if fpath is None:
            raise ToolError(f"Cannot compute hash for missing document: {norm_rel}")
        matched, cur_hash = await asyncio.to_thread(self._find_section, env, fpath, sec_id)
        if matched is None:
            raise ToolError(f"Section not found while pinning: {addr}")
        return RefSpec(kind=RefKind.SECTION, addr=addr, hash=cur_hash)

    async def resolve(self, env: ProbeEnv, spec: RefSpec) -> ResolvedRef:
        fpath, _, sec_id, addr = env.split_fragment(spec.addr)
        if fpath is None:
            return ResolvedRef(
                kind=RefKind.SECTION, status=RefStatus.MISSING, addr=addr, expected_hash=spec.hash
            )
        matched, cur_hash = await asyncio.to_thread(self._find_section, env, fpath, sec_id)
        if matched is None:
            return ResolvedRef(
                kind=RefKind.SECTION, status=RefStatus.MISSING, addr=addr, expected_hash=spec.hash
            )
        return ResolvedRef(
            kind=RefKind.SECTION,
            status=ResolvedRef.status_of(spec.hash, cur_hash),
            addr=addr,
            expected_hash=spec.hash,
            hash=cur_hash,
            heading=matched.heading,
            loc=matched.loc,
            body=matched.body,
        )


class QueryProbe:
    async def prepare(self, env: ProbeEnv, spec: RefSpec, _: str) -> RefSpec:
        cleaned_sql = TabularSandbox.validate_sql(spec.sql)
        reject_if_secret(cleaned_sql, "refs.sql")
        sources: list[RefSource] = []
        for src in spec.sources:
            fpath, norm_rel = env.resolve_file(src.path)
            shash = src.hash
            if not shash:
                if fpath is None:
                    raise ToolError(f"Cannot compute hash for missing source file: {norm_rel}")
                shash = await asyncio.to_thread(env.sha12_file, fpath)
            sources.append(RefSource(path=norm_rel, hash=shash))
        return RefSpec(kind=RefKind.QUERY, sql=cleaned_sql, sources=sources)

    async def resolve(self, env: ProbeEnv, spec: RefSpec) -> ResolvedRef:
        def _sync() -> ResolvedRef:
            cleaned_sql = TabularSandbox.validate_sql(spec.sql)
            reports: list[dict[str, Any]] = []
            pinned: dict[str, Path] = {}
            any_missing = False
            any_stale = False

            for src in spec.sources:
                fpath, norm_rel = env.resolve_file(src.path)
                if fpath is None:
                    any_missing = True
                    reports.append(
                        ResolvedSource(
                            path=norm_rel, status=RefStatus.MISSING, expected_hash=src.hash
                        ).to_dict()
                    )
                    continue
                cur_hash = env.sha12_file(fpath)
                st = ResolvedRef.status_of(src.hash, cur_hash)
                any_stale = any_stale or (st is RefStatus.STALE)
                reports.append(
                    ResolvedSource(
                        path=norm_rel, status=st, expected_hash=src.hash, hash=cur_hash
                    ).to_dict()
                )
                pinned[norm_rel] = fpath

            if any_missing:
                return ResolvedRef(
                    kind=RefKind.QUERY, status=RefStatus.MISSING, sql=cleaned_sql, sources=reports
                )

            rows = env.sandbox.query(cleaned_sql, limit=_RESOLVE_ROW_CAP, pinned_sources=pinned)
            return ResolvedRef(
                kind=RefKind.QUERY,
                status=RefStatus.STALE if any_stale else RefStatus.OK,
                sql=cleaned_sql,
                sources=reports,
                row_count=len(rows),
                rows=rows,
            )

        return await asyncio.to_thread(_sync)


class SymbolProbe:
    @classmethod
    def _strip_docstrings(cls, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
        for child in ast.iter_child_nodes(node):
            cls._strip_docstrings(child)

    @classmethod
    def _ast_hash(cls, node: ast.AST) -> str:
        cloned = copy.deepcopy(node)
        if isinstance(cloned, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            cloned.name = "_"
        cls._strip_docstrings(cloned)
        return ProbeEnv.sha12_text(ast.dump(cloned, annotate_fields=True, include_attributes=False))

    @staticmethod
    def _parse_symbols(fpath: Path) -> dict[str, ast.AST]:
        if fpath.suffix.lower() not in (".py", ".pyi"):
            raise ToolError(
                f"Unsupported language for AST symbol hashing: '{fpath.suffix}' (supported: .py, .pyi)."
            )
        try:
            tree = ast.parse(fpath.read_text(encoding="utf-8"), filename=str(fpath))
        except SyntaxError as exc:
            raise ToolError(f"Syntax error in '{fpath.name}': {exc.msg} (line {exc.lineno})") from None

        found: dict[str, ast.AST] = {}

        def _walk(stmts: Sequence[ast.stmt], prefix: str) -> None:
            for stmt in stmts:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    qual = f"{prefix}.{stmt.name}" if prefix else stmt.name
                    found[qual] = stmt
                    if isinstance(stmt, ast.ClassDef):
                        _walk(stmt.body, qual)

        _walk(tree.body, "")
        return found

    async def prepare(self, env: ProbeEnv, spec: RefSpec, default_rev: str) -> RefSpec:
        fpath, norm_rel, sym_name, addr = env.split_fragment(spec.addr)
        rev = env.validate_rev(spec.rev) if spec.rev else default_rev
        if spec.hash:
            return RefSpec(kind=RefKind.SYMBOL, addr=addr, hash=spec.hash, rev=rev)
        if fpath is None:
            raise ToolError(f"Cannot compute hash for missing code file: {norm_rel}")
        sym_map = await asyncio.to_thread(self._parse_symbols, fpath)
        node = sym_map.get(sym_name)
        if node is None:
            raise ToolError(f"Symbol not found while pinning: {addr}")
        return RefSpec(kind=RefKind.SYMBOL, addr=addr, hash=self._ast_hash(node), rev=rev)

    async def resolve(self, env: ProbeEnv, spec: RefSpec) -> ResolvedRef:
        fpath, norm_rel, sym_name, addr = env.split_fragment(spec.addr)
        if fpath is None:
            cands = await env.git_rename_candidates(spec.rev, norm_rel, sym_name)
            return ResolvedRef(
                kind=RefKind.SYMBOL,
                status=RefStatus.MISSING,
                addr=addr,
                expected_hash=spec.hash,
                rev=spec.rev,
                candidates=cands,
            )
        sym_map = await asyncio.to_thread(self._parse_symbols, fpath)
        node = sym_map.get(sym_name)
        if node is None:
            cands = [
                f"{norm_rel}#{qn}"
                for qn, n in sym_map.items()
                if spec.hash and self._ast_hash(n) == spec.hash
            ]
            return ResolvedRef(
                kind=RefKind.SYMBOL,
                status=RefStatus.MISSING,
                addr=addr,
                expected_hash=spec.hash,
                rev=spec.rev,
                candidates=cands,
            )
        cur_hash = self._ast_hash(node)
        return ResolvedRef(
            kind=RefKind.SYMBOL,
            status=ResolvedRef.status_of(spec.hash, cur_hash),
            addr=addr,
            expected_hash=spec.hash,
            hash=cur_hash,
            rev=spec.rev,
        )


class FileProbe:
    async def prepare(self, env: ProbeEnv, spec: RefSpec, default_rev: str) -> RefSpec:
        fpath, norm_rel = env.resolve_file(spec.addr)
        rev = env.validate_rev(spec.rev) if spec.rev else default_rev
        if spec.hash:
            return RefSpec(kind=RefKind.FILE, addr=norm_rel, hash=spec.hash, rev=rev)
        if fpath is None:
            raise ToolError(f"Cannot compute hash for missing file: {norm_rel}")
        cur_hash = await asyncio.to_thread(env.sha12_file, fpath)
        return RefSpec(kind=RefKind.FILE, addr=norm_rel, hash=cur_hash, rev=rev)

    async def resolve(self, env: ProbeEnv, spec: RefSpec) -> ResolvedRef:
        fpath, norm_rel = env.resolve_file(spec.addr)
        if fpath is None:
            return ResolvedRef(
                kind=RefKind.FILE,
                status=RefStatus.MISSING,
                addr=norm_rel,
                expected_hash=spec.hash,
                rev=spec.rev,
            )
        cur_hash = await asyncio.to_thread(env.sha12_file, fpath)
        return ResolvedRef(
            kind=RefKind.FILE,
            status=ResolvedRef.status_of(spec.hash, cur_hash),
            addr=norm_rel,
            expected_hash=spec.hash,
            hash=cur_hash,
            rev=spec.rev,
        )


class RevisionProbe:
    async def prepare(self, env: ProbeEnv, spec: RefSpec, _: str) -> RefSpec:
        repo_dir, norm_repo = env.resolve_dir(spec.repo or ".")
        rev = env.validate_rev(spec.rev)
        if repo_dir is None:
            raise ToolError(f"Repository directory not found: {norm_repo}")
        rc, full_sha = await env.run_git(
            repo_dir, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"
        )
        if rc != 0:
            raise ToolError(f"Commit revision not found in '{norm_repo}': {rev}")
        return RefSpec(kind=RefKind.REVISION, repo=norm_repo, rev=full_sha[:12])

    async def resolve(self, env: ProbeEnv, spec: RefSpec) -> ResolvedRef:
        repo_dir, norm_repo = env.resolve_dir(spec.repo or ".")
        rev = env.validate_rev(spec.rev)
        if repo_dir is None:
            return ResolvedRef(
                kind=RefKind.REVISION, status=RefStatus.MISSING, repo=norm_repo, rev=rev
            )
        rc, _ = await env.run_git(
            repo_dir, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"
        )
        return ResolvedRef(
            kind=RefKind.REVISION,
            status=RefStatus.OK if rc == 0 else RefStatus.MISSING,
            repo=norm_repo,
            rev=rev,
        )


PROBES: dict[RefKind, RefProbe] = {
    RefKind.SECTION: SectionProbe(),
    RefKind.QUERY: QueryProbe(),
    RefKind.SYMBOL: SymbolProbe(),
    RefKind.FILE: FileProbe(),
    RefKind.REVISION: RevisionProbe(),
}

if set(PROBES) != set(RefKind):
    raise RuntimeError(f"Probe registry mismatch: missing {set(RefKind) - set(PROBES)}")