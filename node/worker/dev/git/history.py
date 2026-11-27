# fiber.node.worker.dev.git.history
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol

__deps__ = {"bins": ["git"]}
GIT = shutil.which("git")
GIT_HINT = "git is not installed or not on PATH (macOS: xcode-select --install, Debian/Ubuntu: apt install git)."

TIMEOUT_SEC = 30
MAX_COMMITS = 200
MAX_BLAME_LINES = 400
_SEP = "\x1f"
_REV_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/~^@{}:+-]*$")  # no leading '-' -> no option injection
_DATE_RE = re.compile(r"^[0-9A-Za-z .:+-]{1,40}$")


async def _git(cwd: Path, *args: str, timeout: int = TIMEOUT_SEC) -> str:
    """Run a read-only git command; non-zero exit -> ToolError with stderr."""
    if GIT is None:
        raise ToolError(GIT_HINT)
    env = {**os.environ, "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    proc = await asyncio.create_subprocess_exec(
        GIT,
        "-c",
        "core.quotepath=off",
        "--no-pager",
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"git {args[0]} timed out after {timeout}s.") from None
    if proc.returncode != 0:
        raise ToolError(f"git {args[0]} failed: {err.decode('utf-8', 'replace').strip()[:1000]}")
    return out.decode("utf-8", "replace")


def _iso(epoch: str) -> str:
    return dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc).isoformat() if epoch.isdigit() else ""


class GitHistoryWorker(AsyncWorkerProtocol):
    def __init__(self, root: str = ".") -> None:
        self.root = Path(root).expanduser().resolve()
        self._repo: Optional[Path] = None
        super().__init__(agent_name="worker.git.history")
        if GIT is None:
            self.log.warning(GIT_HINT)

    async def _repo_root(self) -> Path:
        if GIT is None:
            raise ToolError(GIT_HINT)
        if self._repo is None:
            top = (await _git(self.root, "rev-parse", "--show-toplevel")).strip()
            self._repo = Path(top).resolve()
        return self._repo

    def _rel_path(self, repo: Path, path: str, *, must_be_file: bool = False) -> str:
        clean = path.strip()
        if not clean:
            raise ToolError("Path must not be empty.")
        target = (self.root / clean).resolve()
        if not target.is_relative_to(self.root) or not target.is_relative_to(repo):
            raise ToolError("Path is outside the workspace root / repository.")
        if must_be_file and not target.is_file():
            raise ToolError(f"File not found: {path}")
        return target.relative_to(repo).as_posix() or "."

    @staticmethod
    def _check_rev(rev: str) -> str:
        rev = rev.strip()
        if not _REV_RE.match(rev):
            raise ToolError(f"Invalid revision: {rev!r}")
        return rev

    @tool
    async def search_commits(
        self,
        path: str = "",
        query: str = "",
        author: str = "",
        since: str = "",
        limit: int = 20,
    ) -> Dict[str, Any]:
        """List commits, newest first. `query` finds commits that added or removed that string (git log -S).

        Args:
            path: File or folder relative to the workspace root (default: whole repository).
            query: Exact string whose occurrence count changed in the commit (pickaxe search).
            author: Author name or email substring.
            since: Lower date bound accepted by git (e.g. '2026-01-01', '2 weeks ago').
            limit: Maximum commits to return.
        """
        repo = await self._repo_root()
        capped = max(1, min(limit, MAX_COMMITS))
        args = ["log", f"--format=%H{_SEP}%an{_SEP}%ae{_SEP}%aI{_SEP}%s", f"-n{capped}"]
        if query:
            args.append(f"-S{query}")
        if author:
            args.append(f"--author={author}")
        if since:
            clean_since = since.strip()
            if clean_since.startswith("-") or not _DATE_RE.match(clean_since):
                raise ToolError(f"Invalid 'since': {since!r}")
            args.append(f"--since={clean_since}")

        base_rel = self._rel_path(repo, path) if path.strip() else (
            self.root.relative_to(repo).as_posix() if self.root != repo and self.root.is_relative_to(repo) else ""
        )
        if base_rel and base_rel != ".":
            args += ["--", base_rel]

        commits = []
        for line in (await _git(repo, *args)).splitlines():
            sha, name, mail, date, subject = (line.split(_SEP) + [""] * 5)[:5]
            commits.append({"commit": sha, "author": name, "email": mail, "date": date, "subject": subject})
        if not commits:
            raise ToolError("No matching commits.")
        return {"count": len(commits), "commits": commits}

    @tool
    async def blame(self, path: str, start_line: int = 1, end_line: int = 0) -> Dict[str, Any]:
        """Show which commit last changed each line of a file.

        Args:
            path: File relative to the workspace root.
            start_line: First line (1-based).
            end_line: Last line (0 = start_line + 99).
        """
        repo = await self._repo_root()
        if start_line < 1:
            raise ToolError("'start_line' must be >= 1.")
        end = end_line if end_line != 0 else start_line + 99
        if end < start_line:
            raise ToolError("'end_line' must be >= 'start_line'.")
        end = min(end, start_line + MAX_BLAME_LINES - 1)

        rel_file = self._rel_path(repo, path, must_be_file=True)
        raw = await _git(repo, "blame", "--line-porcelain", f"-L{start_line},{end}", "--", rel_file)
        lines: List[Dict[str, Any]] = []
        meta: Dict[str, str] = {}
        current: Tuple[str, int] = ("", 0)
        for row in raw.splitlines():
            if row.startswith("\t"):
                lines.append({
                    "line": current[1],
                    "commit": current[0][:12],
                    "author": meta.get("author", ""),
                    "date": _iso(meta.get("author-time", "")),
                    "summary": meta.get("summary", ""),
                    "text": row[1:],
                })
                meta = {}
            elif re.match(r"^[0-9a-f]{40} ", row):
                sha, _orig, final, *_ = row.split(" ")
                current = (sha, int(final))
            else:
                key, _, value = row.partition(" ")
                meta[key] = value
        return {"path": path, "lines": lines}

    @tool
    async def show_commit(self, commit: str, path: str = "", max_chars: int = 8000) -> Dict[str, Any]:
        """Show a commit's message, changed files and patch (truncated).

        Args:
            commit: Commit hash or revision (e.g. 'HEAD~1').
            path: Limit the patch to this file or folder (optional).
            max_chars: Maximum patch characters to return.
        """
        repo = await self._repo_root()
        rev = self._check_rev(commit)
        scope = ["--", self._rel_path(repo, path)] if path.strip() else []

        header = await _git(repo, "show", "-s", f"--format=%H{_SEP}%an{_SEP}%aI{_SEP}%B", "--end-of-options", rev)
        sha, author, date, message = (header.split(_SEP, 3) + [""] * 4)[:4]
        stat = await _git(repo, "show", "--format=", "--numstat", "--end-of-options", rev, *scope)
        patch = await _git(repo, "show", "--format=", "--patch", "--end-of-options", rev, *scope)

        files = []
        for row in stat.splitlines():
            if not row.strip():
                continue
            added, deleted, name = (row.split("\t") + ["", "", ""])[:3]
            files.append({
                "path": name,
                "added": None if added == "-" else int(added or 0),
                "deleted": None if deleted == "-" else int(deleted or 0),
            })
        cap = max(500, min(max_chars, 100_000))
        return {
            "commit": sha.strip(),
            "author": author,
            "date": date,
            "message": message.strip(),
            "files": files,
            "patch": patch[:cap],
            "patch_truncated": len(patch) > cap,
        }

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    worker = GitHistoryWorker(root=parser.parse_args().root)
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()