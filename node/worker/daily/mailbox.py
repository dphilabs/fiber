# fiber.node.worker.daily.mailbox
"""@desc: Read-only search over downloaded mail archives (.eml / .mbox) under a workspace root"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import email
import email.policy
import email.utils
from email.message import EmailMessage
from html.parser import HTMLParser
import mailbox
from pathlib import Path
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple

from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol

MAX_SCAN = 50_000        # messages scanned per search call
MAX_RESULTS = 100
SNIPPET_CHARS = 200
_MBOX_SUFFIXES = {".mbox", ".mbx"}
_STRICT_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _HTMLText(HTMLParser):
    """Minimal HTML -> text for mails without a text/plain part."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    return " ".join("".join(parser.parts).split())


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, AssertionError):  # unknown charset / broken encoding
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _body_text(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return _part_text(plain)
    html = msg.get_body(preferencelist=("html",))
    return _html_to_text(_part_text(html)) if html is not None else ""


def _attachments(msg: EmailMessage) -> List[Dict[str, Any]]:
    out = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        out.append({
            "name": part.get_filename() or "(unnamed)",
            "type": part.get_content_type(),
            "bytes": len(payload),
        })
    return out


def _date(msg: EmailMessage) -> Optional[dt.datetime]:
    try:
        parsed = email.utils.parsedate_to_datetime(str(msg.get("Date", "")))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _parse_day(value: str, name: str) -> Optional[dt.datetime]:
    clean = value.strip()
    if not clean:
        return None
    if not _STRICT_DAY_RE.match(clean):
        raise ToolError(f"'{name}' must be strict YYYY-MM-DD (without time or offset), got {value!r}")
    try:
        day = dt.date.fromisoformat(clean)
    except ValueError:
        raise ToolError(f"'{name}' is not a valid calendar date (YYYY-MM-DD): {value!r}") from None
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)


class MailboxWorker(AsyncWorkerProtocol):
    def __init__(self, root: str = ".") -> None:
        self.root = Path(root).expanduser().resolve()
        super().__init__(agent_name="worker.local.mailbox")

    def _resolve(self, rel: str) -> Path:
        clean = rel.strip()
        if not clean:
            raise ToolError("Path must not be empty.")
        target = (self.root / clean).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError("Path is outside the workspace root.")
        if not target.exists():
            raise ToolError(f"Not found: {rel}")
        return target

    def _iter_messages(self, base: Path) -> Iterator[Tuple[str, EmailMessage]]:
        """Yield (message_id, message). id = 'rel.eml' or 'rel.mbox#<index>'."""
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(self.root) or not resolved.is_file():
                continue

            rel = path.relative_to(self.root).as_posix()
            suffix = resolved.suffix.lower()
            if suffix == ".eml":
                with resolved.open("rb") as fh:
                    yield rel, email.message_from_binary_file(fh, policy=email.policy.default)
            elif suffix in _MBOX_SUFFIXES:
                box = mailbox.mbox(resolved, factory=None, create=False)
                try:
                    for idx, key in enumerate(box.iterkeys()):
                        raw = box.get_bytes(key)
                        yield f"{rel}#{idx}", email.message_from_bytes(raw, policy=email.policy.default)
                finally:
                    box.close()

    def _load(self, message_id: str) -> EmailMessage:
        if not message_id:
            raise ToolError("Message id must not be empty.")
        rel, sep, index = message_id.partition("#")
        if not rel.strip():
            raise ToolError(f"Invalid message id: {message_id!r}")

        path = self._resolve(rel)
        if not path.is_file():
            raise ToolError(f"Mail archive is not a file: {rel}")

        suffix = path.suffix.lower()
        if not sep:
            if suffix in _MBOX_SUFFIXES:
                raise ToolError(
                    f"mbox archive requires a '#<index>' suffix (e.g. '{rel}#0'), got {message_id!r}"
                )
            if suffix != ".eml":
                raise ToolError(f"Unsupported mail file extension: {suffix!r}")
            with path.open("rb") as fh:
                return email.message_from_binary_file(fh, policy=email.policy.default)

        if suffix not in _MBOX_SUFFIXES:
            raise ToolError(f"'#<index>' is only valid for .mbox/.mbx archives, got {message_id!r}")
        if not index.isdigit():
            raise ToolError(f"Invalid message id: {message_id!r}")

        box = mailbox.mbox(path, factory=None, create=False)
        try:
            keys = list(box.iterkeys())
            idx_num = int(index)
            if idx_num >= len(keys):
                raise ToolError(f"Message index out of range: {message_id}")
            return email.message_from_bytes(box.get_bytes(keys[idx_num]), policy=email.policy.default)
        finally:
            box.close()

    def _search(
        self,
        base: Path,
        query: str,
        sender: str,
        subject: str,
        since: Optional[dt.datetime],
        until: Optional[dt.datetime],
        cap: int,
    ) -> Dict[str, Any]:
        q, s_from, s_subj = query.lower(), sender.lower(), subject.lower()
        hits: List[Tuple[Optional[dt.datetime], Dict[str, Any]]] = []
        scanned = 0
        for message_id, msg in self._iter_messages(base):
            scanned += 1
            if scanned > MAX_SCAN:
                break
            frm, subj = str(msg.get("From", "")), str(msg.get("Subject", ""))
            when = _date(msg)
            if s_from and s_from not in frm.lower():
                continue
            if s_subj and s_subj not in subj.lower():
                continue
            if since and (when is None or when < since):
                continue
            if until and (when is None or when >= until + dt.timedelta(days=1)):
                continue
            body = _body_text(msg)
            if q and q not in subj.lower() and q not in body.lower():
                continue
            hits.append((
                when,
                {
                    "id": message_id,
                    "date": when.isoformat() if when else None,
                    "from": frm,
                    "to": str(msg.get("To", "")),
                    "subject": subj,
                    "snippet": " ".join(body.split())[:SNIPPET_CHARS],
                    "attachments": [a["name"] for a in _attachments(msg)],
                },
            ))
        hits.sort(key=lambda h: h[0] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
        return {
            "count": len(hits),
            "messages": [h[1] for h in hits[:cap]],
            "truncated": len(hits) > cap,
            "scan_limited": scanned > MAX_SCAN,
        }

    @tool
    async def search_mail(
        self,
        query: str = "",
        sender: str = "",
        subject: str = "",
        since: str = "",
        until: str = "",
        path: str = "",
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Search .eml / .mbox archives by text, sender, subject and date. Newest first.

        Args:
            query: Case-insensitive text matched against subject and body.
            sender: Substring of the From header.
            subject: Substring of the Subject header.
            since: Inclusive start date (YYYY-MM-DD).
            until: Inclusive end date (YYYY-MM-DD).
            path: Folder or archive file relative to the workspace root (default: whole root).
            limit: Maximum messages to return.
        """
        base = self._resolve(path) if path else self.root
        since_dt = _parse_day(since, "since")
        until_dt = _parse_day(until, "until")
        if since_dt and until_dt and since_dt > until_dt:
            raise ToolError("'since' must not be later than 'until'.")

        cap = max(1, min(limit, MAX_RESULTS))
        result = await asyncio.to_thread(
            self._search, base, query, sender, subject, since_dt, until_dt, cap
        )
        if not result["count"]:
            raise ToolError("No matching messages.")
        return result

    @tool
    async def read_mail(self, message_id: str, max_chars: int = 8000) -> Dict[str, Any]:
        """Read one message: headers, text body and attachment list.

        Args:
            message_id: Id returned by search_mail ('a/b.eml' or 'a/box.mbox#3').
            max_chars: Maximum body characters to return.
        """
        msg = await asyncio.to_thread(self._load, message_id.strip())
        body = _body_text(msg)
        cap = max(200, min(max_chars, 50_000))
        when = _date(msg)
        return {
            "id": message_id,
            "date": when.isoformat() if when else None,
            "from": str(msg.get("From", "")),
            "to": str(msg.get("To", "")),
            "cc": str(msg.get("Cc", "")),
            "subject": str(msg.get("Subject", "")),
            "body": body[:cap],
            "body_truncated": len(body) > cap,
            "attachments": _attachments(msg),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    worker = MailboxWorker(root=parser.parse_args().root)
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()