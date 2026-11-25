# fiber.node.bound.reader.runtime
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from xphi.arch.contract.protocol.binder import ToolError

_MAC_SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
_EXTRA_PATH = "/opt/homebrew/bin:/usr/local/bin"


def run_cmd(cmd: List[str], timeout: int = 60) -> str:
    """자식 프로세스를 안전하게 실행하고 타임아웃 시 확실히 종료(kill)하며 실패 시 stderr를 보존합니다."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise ToolError(f"{Path(cmd[0]).name} timed out after {timeout}s.") from None
    if proc.returncode != 0:
        raise ToolError(f"{Path(cmd[0]).name} failed: {(err or '').strip()[-600:]}")
    return out or ""


@dataclass(frozen=True)
class ReaderRuntimeOpts:
    ocr_lang: str = "kor+eng"
    tesseract_bin: Optional[str] = None
    soffice_bin: Optional[str] = None
    pandoc_bin: Optional[str] = None
    tesseract_langs: Set[str] = field(default_factory=set)
    fts_trigram: bool = False

    @classmethod
    def from_daemon_env(cls) -> "ReaderRuntimeOpts":
        raw = os.environ.get("FIBER_READER_OPTS", "").strip()
        kv: Dict[str, str] = {}
        for tok in re.split(r"[,\s]+", raw) if raw else ():
            if "=" in tok:
                k, _, v = tok.partition("=")
                kv[k.strip().lower()] = v.strip()

        base_path = os.environ.get("PATH", "")
        search_path = f"{base_path}:{_EXTRA_PATH}" if base_path else _EXTRA_PATH

        def _pick(key: str, names: Sequence[str], fallback: str = "") -> Optional[str]:
            val = kv.get(key, "auto")
            if val.lower() in ("off", "none", "0", "false"):
                return None
            if val != "auto":
                return val if (os.path.isfile(val) and os.access(val, os.X_OK)) else shutil.which(val, path=search_path)
            for n in names:
                if found := shutil.which(n, path=search_path):
                    return found
            return fallback if (fallback and os.path.isfile(fallback) and os.access(fallback, os.X_OK)) else None

        ocr_cfg = kv.get("ocr", "kor+eng")
        ocr_off = ocr_cfg.lower() in ("off", "none", "0", "false")
        tess_bin = None if ocr_off else _pick("tesseract", ("tesseract",))
        soffice_bin = None if kv.get("office", "auto").lower() in ("off", "0", "false") else _pick(
            "soffice", ("soffice", "libreoffice"), _MAC_SOFFICE
        )
        pandoc_bin = None if kv.get("pandoc", "auto").lower() in ("off", "0", "false") else _pick("pandoc", ("pandoc",))

        tess_langs: Set[str] = set()
        if tess_bin:
            try:
                out = run_cmd([tess_bin, "--list-langs"], timeout=5)
                tess_langs = {ln.strip() for ln in out.splitlines()[1:] if ln.strip()}
            except Exception:
                pass

        fts_trigram = False
        try:
            with sqlite3.connect(":memory:") as db:
                db.execute("CREATE VIRTUAL TABLE _t USING fts5(x, tokenize='trigram');")
                fts_trigram = True
        except sqlite3.OperationalError:
            pass

        return cls(
            ocr_lang="off" if ocr_off else ocr_cfg,
            tesseract_bin=tess_bin,
            soffice_bin=soffice_bin,
            pandoc_bin=pandoc_bin,
            tesseract_langs=tess_langs,
            fts_trigram=fts_trigram,
        )