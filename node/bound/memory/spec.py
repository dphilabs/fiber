# fiber.node.bound.memory.spec
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fiber.gateway.bridge.action.sign import (
    LOCAL_CIPHER_KEY_PATH,
    _normalize_spiffe_id,
    ensure_node_keys,
)
from xphi.arch.bound.adapter.pta import NodeSigner
from xphi.node.space.bind.resolver import resolve_path

MAX_NOTE_CHARS = 8_000
MAX_SQL_CHARS = 4_000
MAX_REFS_PER_NOTE = 20


class RefKind(StrEnum):
    SECTION = "section"
    QUERY = "query"
    SYMBOL = "symbol"
    FILE = "file"
    REVISION = "revision"

    @property
    def has_fragment(self) -> bool:
        return self in (RefKind.SECTION, RefKind.SYMBOL)

    @property
    def tracks_git_rev(self) -> bool:
        return self in (RefKind.SYMBOL, RefKind.FILE)


class RefStatus(StrEnum):
    OK = "OK"
    STALE = "STALE"
    MISSING = "MISSING"


class MemoryIntegrityError(RuntimeError):
    """Raised when stored note or reference fails HMAC / AEAD verification."""


def _resolve_default_account() -> str:
    raw = (
        os.environ.get("FIBER_ACCOUNT", "").strip()
        or getpass.getuser().strip()
        or "fiber"
    )
    return _normalize_spiffe_id(raw)


@dataclass(frozen=True, slots=True)
class MemoryIdentity:
    account: str
    pubkey_hex: str
    db_path: Path
    _domain_key: bytes = field(repr=False)

    @classmethod
    def bind(cls, account: str | None = None, db_path: str = "") -> MemoryIdentity:
        ensure_node_keys()
        signer = NodeSigner.get_instance()
        pubkey_hex = signer.pubkey_hex

        canonical_account = (
            _normalize_spiffe_id(account.strip())
            if account and account.strip()
            else _resolve_default_account()
        )

        raw_seed = LOCAL_CIPHER_KEY_PATH.read_text(encoding="utf-8").strip()
        ikm = bytes.fromhex(raw_seed) if len(raw_seed) == 64 else raw_seed.encode("utf-8")
        ctx = f"dphi:domain:user_memory:v1:{pubkey_hex}:{canonical_account}".encode("utf-8")
        domain_key = hmac.new(ikm, ctx, hashlib.sha256).digest()

        if db_path and db_path.strip():
            resolved_db = Path(db_path).expanduser().resolve()
            resolved_db.parent.mkdir(parents=True, exist_ok=True)
        else:
            scope_hash = hashlib.sha256(
                f"{canonical_account}:{pubkey_hex}".encode("utf-8")
            ).hexdigest()[:16]
            mem_dir = Path(str(resolve_path("anchor"))) / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(mem_dir, 0o700)
            except OSError:
                pass
            resolved_db = mem_dir / f"{scope_hash}.db"

        return cls(
            account=canonical_account,
            pubkey_hex=pubkey_hex,
            db_path=resolved_db,
            _domain_key=domain_key,
        )

    def sign_note_row(
        self, project: str, key: str, tags: str, note: str, ref_count: int
    ) -> str:
        payload = (
            f"{self.account}\x1f{project}\x1f{key}\x1f{tags}\x1f{ref_count}\x1f{note}"
        ).encode("utf-8")
        return hmac.new(self._domain_key, payload, hashlib.sha256).hexdigest()

    def verify_note_row(
        self, project: str, key: str, tags: str, note: str, ref_count: int, sig: str
    ) -> None:
        expected = self.sign_note_row(project, key, tags, note, ref_count)
        if not sig or not hmac.compare_digest(expected, sig):
            raise MemoryIntegrityError(
                f"Note integrity signature mismatch for ({project}, {key})."
            )

    def seal_ref(self, project: str, key: str, ord_idx: int, spec_json: str) -> str:
        nonce = os.urandom(12)
        aad = f"{self.account}|{project}|{key}|{ord_idx}".encode("utf-8")
        ct = AESGCM(self._domain_key).encrypt(nonce, spec_json.encode("utf-8"), aad)
        return (nonce + ct).hex()

    def open_ref(self, project: str, key: str, ord_idx: int, cipher_hex: str) -> str:
        try:
            raw = bytes.fromhex(cipher_hex)
            if len(raw) < 28:
                raise ValueError("Ciphertext too short.")
            aad = f"{self.account}|{project}|{key}|{ord_idx}".encode("utf-8")
            pt = AESGCM(self._domain_key).decrypt(raw[:12], raw[12:], aad)
            return pt.decode("utf-8")
        except (ValueError, InvalidTag) as exc:
            raise MemoryIntegrityError(
                f"Reference integrity verification failed for ({project}, {key})[#{ord_idx}]."
            ) from exc

    def blind_target(self, project: str, norm_addr: str) -> str:
        payload = f"target\x1f{self.account}\x1f{project}\x1f{norm_addr}".encode("utf-8")
        return hmac.new(self._domain_key, payload, hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class RefSource:
    path: str
    hash: str = ""


@dataclass(frozen=True, slots=True)
class RefSpec:
    kind: RefKind
    addr: str = ""
    hash: str = ""
    rev: str = ""
    repo: str = ""
    sql: str = ""
    sources: list[RefSource] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RefSpec:
        if not isinstance(raw, dict):
            raise ValueError("Each ref entry must be an object.")
        raw_kind = str(raw.get("kind", "")).strip()
        try:
            kind = RefKind(raw_kind)
        except ValueError:
            expected = tuple(k.value for k in RefKind)
            raise ValueError(
                f"Unsupported ref kind: {raw_kind!r} (expected one of {expected})."
            ) from None

        match kind:
            case RefKind.SECTION:
                addr = str(raw.get("addr", "")).strip()
                if not addr or "#" not in addr:
                    raise ValueError("Section ref requires 'addr' in '<doc_path>#<section_id>' format.")
                return cls(kind=kind, addr=addr, hash=str(raw.get("hash", "")).strip())

            case RefKind.QUERY:
                sql = str(raw.get("sql", "")).strip()
                if not sql:
                    raise ValueError("Query ref requires non-empty 'sql'.")
                if len(sql) > MAX_SQL_CHARS:
                    raise ValueError(f"Query ref 'sql' exceeds {MAX_SQL_CHARS} chars.")
                raw_srcs = raw.get("sources") or []
                if not isinstance(raw_srcs, list) or not raw_srcs:
                    raise ValueError("Query ref requires non-empty 'sources' list.")
                srcs = [
                    RefSource(path=str(s["path"]).strip(), hash=str(s.get("hash", "")).strip())
                    for s in raw_srcs
                    if isinstance(s, dict) and str(s.get("path", "")).strip()
                ]
                if not srcs:
                    raise ValueError(
                        "Query ref 'sources' must contain at least one {'path': ...} object."
                    )
                return cls(kind=kind, sql=sql, sources=srcs)

            case RefKind.SYMBOL:
                addr = str(raw.get("addr", "")).strip()
                if not addr or "#" not in addr:
                    raise ValueError("Symbol ref requires 'addr' in '<file_path>#<symbol_name>' format.")
                return cls(
                    kind=kind,
                    addr=addr,
                    hash=str(raw.get("hash", "")).strip(),
                    rev=str(raw.get("rev", "")).strip(),
                )

            case RefKind.FILE:
                addr = str(raw.get("addr", "")).strip()
                if not addr or "#" in addr:
                    raise ValueError("File ref requires non-empty 'addr' without '#' fragment.")
                return cls(
                    kind=kind,
                    addr=addr,
                    hash=str(raw.get("hash", "")).strip(),
                    rev=str(raw.get("rev", "")).strip(),
                )

            case RefKind.REVISION:
                rev = str(raw.get("rev", "")).strip()
                if not rev:
                    raise ValueError("Revision ref requires non-empty 'rev'.")
                repo = str(raw.get("repo", ".")).strip() or "."
                return cls(kind=kind, repo=repo, rev=rev)

    def target_addrs(self) -> list[str]:
        out: list[str] = []
        if self.kind.has_fragment and self.addr:
            out.append(self.addr)
            file_part, _, _ = self.addr.partition("#")
            if file_part and file_part not in out:
                out.append(file_part)
        elif self.kind is RefKind.FILE and self.addr:
            out.append(self.addr)
        elif self.kind is RefKind.QUERY:
            for src in self.sources:
                if src.path and src.path not in out:
                    out.append(src.path)
        return out

    def to_dict(self) -> dict[str, Any]:
        match self.kind:
            case RefKind.SECTION:
                return {"kind": self.kind.value, "addr": self.addr, "hash": self.hash}
            case RefKind.QUERY:
                return {
                    "kind": self.kind.value,
                    "sql": self.sql,
                    "sources": [asdict(s) for s in self.sources],
                }
            case RefKind.SYMBOL | RefKind.FILE:
                d: dict[str, Any] = {"kind": self.kind.value, "addr": self.addr, "hash": self.hash}
                if self.rev:
                    d["rev"] = self.rev
                return d
            case RefKind.REVISION:
                return {"kind": self.kind.value, "repo": self.repo or ".", "rev": self.rev}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> RefSpec:
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True, slots=True)
class NoteRecord:
    key: str
    note: str
    project: str = "default"
    tags: str = ""
    refs: list[RefSpec] = field(default_factory=list)
    account: str = ""
    updated_at: str = ""

    def validate_limits(self) -> None:
        if not self.key.strip():
            raise ValueError("'key' must not be empty.")
        if not self.note.strip():
            raise ValueError("'note' must not be empty.")
        if len(self.note) > MAX_NOTE_CHARS:
            raise ValueError(f"'note' exceeds maximum length ({MAX_NOTE_CHARS} chars).")
        if len(self.refs) > MAX_REFS_PER_NOTE:
            raise ValueError(f"'refs' exceeds maximum count ({MAX_REFS_PER_NOTE}).")

    @property
    def ref_count(self) -> int:
        return len(self.refs)

@dataclass(frozen=True, slots=True)
class NoteSummary:
    key: str
    project: str
    tags: str
    preview: str
    ref_count: int
    updated_at: str