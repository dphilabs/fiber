# fiber.node.bound.reader.parsers
from __future__ import annotations

import email
import email.policy
import mailbox
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, List, Set, Tuple

import pdfplumber

from fiber.node.bound.reader.runtime import ReaderRuntimeOpts, run_cmd
from fiber.node.bound.reader.tables import table_to_markdown

SUPPORTED_EXTS = {
    ".md", ".markdown", ".txt", ".docx", ".hwpx", ".pdf",
    ".eml", ".mbox", ".mbx",
    ".hwp", ".doc", ".ppt", ".pptx", ".rtf", ".odt",
    ".epub", ".rst", ".org", ".tex",
}

_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_SLUG_RE = re.compile(r"[^0-9a-zA-Z가-힣]+")
_TAG = lambda el: el.tag.rsplit("}", 1)[-1]


@dataclass
class ParsedSection:
    section_id: str
    heading: str
    level: int
    loc: str
    body: str
    tables: List[List[List[str]]]
    flags: List[str]


class _HTMLText(HTMLParser):
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


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, AssertionError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _mail_body(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return _part_text(plain).strip()
    html = msg.get_body(preferencelist=("html",))
    if html is None:
        return ""
    parser = _HTMLText()
    parser.feed(_part_text(html))
    return "\n".join(ln.strip() for ln in "".join(parser.parts).splitlines() if ln.strip())


def _next_id(heading: str, idx: int, used: Set[str]) -> str:
    base = _SLUG_RE.sub("-", heading.strip().lower()).strip("-")[:48] or f"sec-{idx}"
    sec_id, n = base, 2
    while sec_id in used:
        sec_id, n = f"{base}-{n}", n + 1
    used.add(sec_id)
    return sec_id


def parse_markdown(text: str) -> List[ParsedSection]:
    lines = text.splitlines()
    secs: List[ParsedSection] = []
    heading, level, start = "Overview", 1, 1
    buf: List[str] = []
    in_fence, used = False, set()

    def _flush(end: int) -> None:
        body = "\n".join(buf).strip()
        if not body and secs:
            return
        tbls, cur = [], []
        for ln in body.splitlines():
            s = ln.strip()
            if s.startswith("|") and s.endswith("|"):
                cells = [c.strip() for c in s[1:-1].split("|")]
                if not all(set(c) <= {"-", ":", " "} for c in cells):
                    cur.append(cells)
            elif cur:
                if len(cur) >= 2:
                    tbls.append(cur)
                cur = []
        if len(cur) >= 2:
            tbls.append(cur)
        secs.append(ParsedSection(_next_id(heading, len(secs) + 1, used), heading, level, f"lines {start}-{max(start, end)}", body, tbls, []))

    for i, ln in enumerate(lines, 1):
        if ln.strip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and (m := _MD_HEADING_RE.match(ln)):
            if buf:
                _flush(i - 1)
                buf = []
            level, heading, start = len(m.group(1)), m.group(2).strip(), i
            continue
        buf.append(ln)
    _flush(len(lines))
    return secs


def parse_docx(path: Path) -> List[ParsedSection]:
    with zipfile.ZipFile(path, "r") as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body_el = next((el for el in root.iter() if _TAG(el) == "body"), root)

    secs: List[ParsedSection] = []
    heading, level, idx, start = "Document", 1, 0, 1
    buf, tbls, used = [], [], set()

    def _flush(end: int) -> None:
        txt = "\n".join(buf).strip()
        if txt or tbls:
            secs.append(ParsedSection(_next_id(heading, len(secs) + 1, used), heading, level, f"blocks {start}-{max(start, end)}", txt, list(tbls), []))

    for child in list(body_el):
        t = _TAG(child)
        if t == "p":
            idx += 1
            style = ""
            parts: List[str] = []
            for sub in child.iter():
                if _TAG(sub) == "pStyle":
                    style = next((v.lower() for k, v in sub.attrib.items() if k.endswith("val")), "")
                elif _TAG(sub) == "t" and sub.text:
                    parts.append(sub.text)
            line = "".join(parts).strip()
            if not line:
                continue
            if style.startswith("heading") or style in ("1", "2", "3", "제목1", "제목2", "제목3"):
                if buf or tbls:
                    _flush(idx - 1)
                    buf, tbls = [], []
                digits = "".join(c for c in style if c.isdigit())
                level = min(3, max(1, int(digits))) if digits else 1
                heading, start = line, idx
            else:
                buf.append(line)
        elif t == "tbl":
            idx += 1
            rows: List[List[str]] = []
            for tr in child:
                if _TAG(tr) != "tr":
                    continue
                rc: List[str] = []
                for tc in tr:
                    if _TAG(tc) != "tc":
                        continue
                    txt = " ".join(x.text.strip() for x in tc.iter() if _TAG(x) == "t" and x.text and x.text.strip())
                    span, vcont = 1, False
                    for pr in tc.iter():
                        if _TAG(pr) == "gridSpan":
                            v = next((v for k, v in pr.attrib.items() if k.endswith("val")), "1")
                            span = max(1, int(v)) if v.isdigit() else 1
                        elif _TAG(pr) == "vMerge":
                            if next((v for k, v in pr.attrib.items() if k.endswith("val")), "continue") != "restart":
                                vcont = True
                    rc.extend(["" if vcont else txt] + [""] * (span - 1))
                if rc:
                    rows.append(rc)
            if rows:
                tbls.append(rows)
                buf.append(table_to_markdown(rows))
    _flush(max(1, idx))
    return secs


def parse_hwpx(path: Path) -> List[ParsedSection]:
    secs: List[ParsedSection] = []
    with zipfile.ZipFile(path, "r") as zf:
        names = sorted(n for n in zf.namelist() if n.startswith("Contents/section") and n.endswith(".xml"))
        for idx, name in enumerate(names, 1):
            root = ET.fromstring(zf.read(name))
            paras, tbls = [], []
            for el in root.iter():
                if _TAG(el) == "tbl":
                    rows = [
                        [" ".join(t.text.strip() for t in tc.iter() if _TAG(t) == "t" and t.text and t.text.strip()) for tc in tr if _TAG(tc) == "tc"]
                        for tr in el if _TAG(tr) == "tr"
                    ]
                    if rows := [r for r in rows if r]:
                        tbls.append(rows)
                elif _TAG(el) == "p":
                    if txt := "".join(t.text for t in el.iter() if _TAG(t) == "t" and t.text).strip():
                        paras.append(txt)
            if paras or tbls:
                secs.append(ParsedSection(f"sec-{idx}", (paras[0][:80] if paras else f"Section {idx}"), 1, name, "\n".join(paras), tbls, []))
    return secs


def _ocr_page(page: Any, opts: ReaderRuntimeOpts) -> Tuple[str, str]:
    if not opts.tesseract_bin or opts.ocr_lang == "off":
        return "", "UNREADABLE_SCAN_PAGE"
    req = {x.strip() for x in opts.ocr_lang.split("+") if x.strip()}
    if opts.tesseract_langs and not req.issubset(opts.tesseract_langs):
        return "", f"UNREADABLE_SCAN_PAGE(missing_lang={','.join(sorted(req - opts.tesseract_langs))})"
    try:
        img = page.to_image(resolution=200).original
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            p = Path(tmp.name)
        try:
            img.save(p)
            txt = run_cmd([opts.tesseract_bin, str(p), "stdout", "-l", opts.ocr_lang], timeout=30).strip()
            return (txt, "OCR_FALLBACK") if txt else ("", "UNREADABLE_SCAN_PAGE")
        finally:
            p.unlink(missing_ok=True)
    except Exception:
        return "", "UNREADABLE_SCAN_PAGE"


def parse_pdf(path: Path, opts: ReaderRuntimeOpts) -> List[ParsedSection]:
    secs: List[ParsedSection] = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            flags: List[str] = []
            txt = (page.extract_text() or "").strip()
            tbls = [[[c or "" for c in r] for r in t] for t in (page.extract_tables() or []) if t and len(t) >= 2]
            if len(txt) < 25 and (page.images or not tbls):
                ocr_txt, flg = _ocr_page(page, opts)
                flags.append(f"{flg}:p.{pno}")
                if ocr_txt:
                    txt = ocr_txt
            for t in tbls:
                if (md := table_to_markdown(t)) and md not in txt:
                    txt = f"{txt}\n\n{md}".strip()
            if txt or tbls or flags:
                head = next((ln.strip() for ln in txt.splitlines() if ln.strip()), f"Page {pno}")[:80]
                secs.append(ParsedSection(f"p-{pno}", head, 1, f"p.{pno}", txt or f"[{', '.join(flags)}]", tbls, flags))
    return secs


def parse_mail(path: Path) -> List[ParsedSection]:
    def _one(sid: str, loc: str, m: EmailMessage) -> ParsedSection:
        subj = str(m.get("Subject", "(no subject)")).strip()
        meta_lines = [
            f"From: {m.get('From', '')}",
            f"To: {m.get('To', '')}",
            f"Date: {m.get('Date', '')}",
            f"Subject: {subj}",
        ]
        atts = [p.get_filename() or "(unnamed)" for p in m.iter_attachments()]
        if atts:
            meta_lines.append(f"Attachments: {', '.join(atts)}")
        full = "\n".join(meta_lines) + f"\n\n{_mail_body(m)}"
        return ParsedSection(sid, subj[:80] or sid, 1, loc, full.strip(), [], [])

    if path.suffix.lower() == ".eml":
        with path.open("rb") as f:
            return [_one("msg-0", "eml", email.message_from_binary_file(f, policy=email.policy.default))]
    box = mailbox.mbox(path, factory=None, create=False)
    try:
        return [
            _one(f"msg-{i}", f"mbox#{i}", email.message_from_bytes(box.get_bytes(k), policy=email.policy.default))
            for i, k in zip(range(5000), box.iterkeys())
        ]
    finally:
        box.close()


def parse_document(path: Path, opts: ReaderRuntimeOpts) -> List[ParsedSection]:
    ext = path.suffix.lower()
    if ext in (".md", ".markdown", ".txt"):
        return parse_markdown(path.read_text(encoding="utf-8", errors="replace"))
    if ext == ".docx":
        return parse_docx(path)
    if ext == ".hwpx":
        return parse_hwpx(path)
    if ext == ".pdf":
        return parse_pdf(path, opts)
    if ext in (".eml", ".mbox", ".mbx"):
        return parse_mail(path)
    if ext in (".epub", ".rst", ".org", ".tex"):
        if not opts.pandoc_bin:
            return [ParsedSection("sec-1", path.name, 1, "p.1", "[UNSUPPORTED_WITHOUT_PANDOC]", [], ["UNSUPPORTED_WITHOUT_PANDOC"])]
        md = run_cmd([opts.pandoc_bin, "--to", "gfm", "--", str(path)], timeout=60)
        secs = parse_markdown(md)
        for s in secs:
            s.flags.append("PANDOC_FALLBACK")
        return secs
    if ext in (".hwp", ".doc", ".ppt", ".pptx", ".rtf", ".odt"):
        if not opts.soffice_bin:
            return [ParsedSection("sec-1", path.name, 1, "p.1", "[UNSUPPORTED_WITHOUT_SOFFICE]", [], ["UNSUPPORTED_WITHOUT_SOFFICE"])]
        with tempfile.TemporaryDirectory(prefix="reader_lo_") as tmp:
            work = Path(tmp)
            run_cmd(
                [opts.soffice_bin, f"-env:UserInstallation={(work / 'prof').as_uri()}", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(work), str(path)],
                timeout=120,
            )
            pdf_out = work / f"{path.stem}.pdf"
            if not pdf_out.is_file():
                return [ParsedSection("sec-1", path.name, 1, "p.1", "[CONVERSION_FAILED]", [], ["CONVERSION_FAILED"])]
            secs = parse_pdf(pdf_out, opts)
            for s in secs:
                s.flags.append("SOFFICE_FALLBACK")
            return secs
    return []