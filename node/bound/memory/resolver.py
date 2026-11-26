# fiber.node.bound.memory.resolver
from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
from typing import Any

from fiber.node.bound.memory.probes import PROBES, ProbeEnv, reject_if_secret
from fiber.node.bound.memory.spec import NoteRecord, RefSpec, RefStatus
from fiber.node.bound.reader.runtime import ReaderRuntimeOpts
from xphi.arch.contract.protocol.binder import ToolError

_JSON_VALUE_TAG = "__type:json"


def encode_pin_value(value: str | dict[str, Any], user_tags: str) -> tuple[str, str]:
    tag_list = [
        t.strip() for t in (user_tags or "").split(",") if t.strip() and t.strip() != _JSON_VALUE_TAG
    ]
    if isinstance(value, dict):
        if not value:
            raise ValueError("'value' object must not be empty.")
        reject_if_secret(value, "value")
        return (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            ",".join([*tag_list, _JSON_VALUE_TAG]),
        )
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("'value' string must not be empty.")
        reject_if_secret(cleaned, "value")
        return cleaned, ",".join(tag_list)
    raise ValueError("'value' must be either a non-empty string or a JSON object.")


def decode_pin_value(note_text: str, raw_tags: str) -> str | dict[str, Any]:
    if _JSON_VALUE_TAG in {t.strip() for t in (raw_tags or "").split(",") if t.strip()}:
        try:
            parsed = json.loads(note_text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return note_text


def public_tags(raw_tags: str) -> str:
    return ",".join(
        t.strip() for t in (raw_tags or "").split(",") if t.strip() and t.strip() != _JSON_VALUE_TAG
    )


class MemoryResolver:
    def __init__(
        self, root: str | Path = ".", reader_opts: ReaderRuntimeOpts | None = None
    ) -> None:
        ws_root = Path(root).expanduser().resolve()
        opts = reader_opts or ReaderRuntimeOpts.from_daemon_env()
        self.root = ws_root
        self.env = ProbeEnv(ws_root, opts)

    def normalize_scope_addr(self, raw_addr: str) -> str:
        clean = raw_addr.strip()
        if not clean:
            raise ValueError("Scope address must not be empty.")
        if "#" in clean:
            _, _, frag, canon = self.env.split_fragment(clean)
            if not frag:
                raise ValueError(f"Missing fragment after '#' in address: {raw_addr!r}")
            return canon
        _, norm_rel = self.env.resolve_file(clean)
        return norm_rel

    async def current_head_rev(self) -> str:
        return await self.env.head_rev()

    async def prepare_all(self, raw_refs: Sequence[dict[str, Any]]) -> list[RefSpec]:
        if not raw_refs:
            return []
        specs = [RefSpec.from_dict(r) for r in raw_refs]
        needs_rev = any(s.kind.tracks_git_rev and not s.rev for s in specs)
        default_rev = await self.env.head_rev() if needs_rev else ""
        out: list[RefSpec] = []
        for spec in specs:
            probe = PROBES.get(spec.kind)
            if probe is None:
                raise ValueError(f"Unsupported ref kind: {spec.kind!r}")
            out.append(await probe.prepare(self.env, spec, default_rev))
        return out

    async def resolve_record(self, rec: NoteRecord) -> dict[str, Any]:
        resolved_refs: list[dict[str, Any]] = []
        for spec in rec.refs:
            probe = PROBES.get(spec.kind)
            if probe is None:
                raise ToolError(f"Unsupported stored ref kind: {spec.kind!r}")
            resolved_refs.append((await probe.resolve(self.env, spec)).to_dict())

        return {
            "key": rec.key,
            "project": rec.project,
            "value": decode_pin_value(rec.note, rec.tags),
            "tags": public_tags(rec.tags),
            "refs": resolved_refs,
            "ok": all(r["status"] == RefStatus.OK for r in resolved_refs),
            "updated_at": rec.updated_at,
        }