# fiber.node.worker.dev.ast
import asyncio
import difflib
from pathlib import Path
from typing import Any, Dict, List, Literal

from ast_grep_py import SgRoot

from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol

__deps__ = {"py": ["ast_grep_py"],}
SupportedLang = Literal["python", "typescript", "javascript", "rust", "go"]
_OUTLINE_KINDS: Dict[str, tuple[str, ...]] = {
    "python": ("class_definition", "function_definition", "decorated_definition"),
    "typescript": ("class_declaration", "function_declaration", "interface_declaration", "method_definition"),
    "javascript": ("class_declaration", "function_declaration", "method_definition"),
    "rust": ("struct_item", "enum_item", "trait_item", "impl_item", "function_item"),
    "go": ("type_declaration", "function_declaration", "method_declaration"),
}


class AstWorker(AsyncWorkerProtocol):
    def __init__(self, workspace_root: str = ".", max_matches: int = 100):
        self.workspace_root = Path(workspace_root).resolve()
        self.max_matches = max_matches
        super().__init__(agent_name="worker.ast")

    def _iter_workspace_files(self, glob_pattern: str) -> List[Path]:
        files: List[Path] = []
        for p in sorted(self.workspace_root.glob(glob_pattern)):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(self.workspace_root).parts
            if any(part.startswith(".") or part in ("node_modules", "__pycache__", "venv", ".venv") for part in rel_parts):
                continue
            files.append(p)
        return files

    @tool
    async def code_outline(self, path: str, lang: SupportedLang = "python") -> Dict[str, Any]:
        """소스 파일 전체를 읽지 않고 클래스·함수 선언부의 시그니처와 라인 범위만 추출합니다.

        Args:
            path: 워크스페이스 기준 상대 파일 경로
            lang: 대상 코드 언어 (python, typescript, javascript, rust, go)
        """
        target = (self.workspace_root / path).resolve()
        if not target.is_relative_to(self.workspace_root) or not target.is_file():
            raise ToolError(f"Invalid or missing file path: {path}")

        def _extract() -> List[Dict[str, Any]]:
            src = target.read_text(encoding="utf-8", errors="replace")
            sg = SgRoot(src, lang)
            root = sg.root()
            target_kinds = _OUTLINE_KINDS.get(lang, ())
            symbols: List[Dict[str, Any]] = []

            for node in root.children():
                if node.kind() in target_kinds:
                    rng = node.range()
                    first_line = node.text().splitlines()[0].strip()
                    symbols.append({
                        "kind": node.kind(),
                        "start_line": rng.start.line + 1,
                        "end_line": rng.end.line + 1,
                        "signature": first_line[:160],
                    })
            return symbols

        outline = await asyncio.to_thread(_extract)
        return {"path": path, "symbol_count": len(outline), "symbols": outline}

    @tool
    async def ast_search(
        self, pattern: str, lang: SupportedLang = "python", glob: str = "**/*.py"
    ) -> Dict[str, Any]:
        """AST 구문 패턴으로 워크스페이스 내 일치하는 코드 위치를 검색합니다.

        Args:
            pattern: ast-grep 검색 패턴 (예: 'def $FUNC($$$ARGS): $$$BODY')
            lang: 대상 코드 언어
            glob: 대상 파일 글롭 패턴 (예: 'src/**/*.py')
        """
        def _search() -> List[Dict[str, Any]]:
            matches: List[Dict[str, Any]] = []
            for file_path in self._iter_workspace_files(glob):
                src = file_path.read_text(encoding="utf-8", errors="replace")
                sg = SgRoot(src, lang)
                for node in sg.root().find_all(pattern=pattern):
                    rng = node.range()
                    matches.append({
                        "file": file_path.relative_to(self.workspace_root).as_posix(),
                        "start_line": rng.start.line + 1,
                        "end_line": rng.end.line + 1,
                        "snippet": node.text(),
                    })
                    if len(matches) >= self.max_matches:
                        return matches
            return matches

        try:
            hits = await asyncio.to_thread(_search)
        except Exception as e:
            raise ToolError(f"AST search failed: {e}")

        return {"count": len(hits), "truncated": len(hits) >= self.max_matches, "matches": hits}

    @tool
    async def ast_replace(
        self,
        pattern: str,
        rewrite: str,
        lang: SupportedLang = "python",
        glob: str = "**/*.py",
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """AST 구문 패턴을 찾아 일괄 치환합니다. 기본적으로 dry_run=True로 Unified Diff만 먼저 반환합니다.

        Args:
            pattern: 찾을 AST 구문 패턴
            rewrite: 치환할 AST 템플릿
            lang: 대상 코드 언어
            glob: 대상 파일 글롭 패턴
            dry_run: True면 파일을 수정하지 않고 변경 Diff만 반환, False면 디스크에 실제 반영
        """
        def _replace() -> Dict[str, Any]:
            diffs: List[str] = []
            modified_files: List[str] = []

            for file_path in self._iter_workspace_files(glob):
                orig = file_path.read_text(encoding="utf-8", errors="replace")
                sg = SgRoot(orig, lang)
                root = sg.root()
                found = root.find_all(pattern=pattern)
                if not found:
                    continue

                edits = [m.replace(rewrite) for m in found]
                updated = root.commit_edits(edits)
                if updated == orig:
                    continue

                rel = file_path.relative_to(self.workspace_root).as_posix()
                modified_files.append(rel)
                diff_text = "\n".join(
                    difflib.unified_diff(
                        orig.splitlines(),
                        updated.splitlines(),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                        lineterm="",
                    )
                )
                diffs.append(diff_text)

                if not dry_run:
                    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
                    tmp.write_text(updated, encoding="utf-8")
                    tmp.replace(file_path)

            return {
                "dry_run": dry_run,
                "modified_count": len(modified_files),
                "files": modified_files,
                "diff": "\n\n".join(diffs)[:8000],
            }

        try:
            return await asyncio.to_thread(_replace)
        except Exception as e:
            raise ToolError(f"AST replace failed: {e}")


def main():
    worker = AstWorker()
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        worker.log.info("AstWorker shutting down.")


if __name__ == "__main__":
    main()