# fiber.studio.console.runbook.frame.loader
"""Runbook viewers in isolated iframes.

    frame/loader.py                            this module (Python wrappers)
    frame/viewer/doc.html + doc.js             Doc viewer: spec, dry run result, run trace, template (read-only)
    frame/viewer/dag.html + dag.js + dagre.min.js
                                               DAG viewer: draft wiring as a graph, slot source dropdown

Each html references its js with ``<script src="...">``, so a viewer can be opened directly in a browser.
The console iframe uses ``srcdoc``, which cannot load files, so the referenced js is inlined when the
srcdoc is built. Files are read on every build, so edits show up without restarting the app.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
from pathlib import Path
import re
from typing import Any

from nicegui import ui

# The html/js files live in the `viewer/` folder next to this module.
VIEWER_DIR = Path(__file__).resolve().parent / "viewer"

_SCRIPT_SRC = re.compile(r'<script src="([\w.\-]+)"></script>')
_MODEL_MARK = "/*__MODEL__*/null"
_INTENT_EVENT = "runbook_dag_intent"

# Forwards DAG viewer intents (iframe -> parent page) to Python, tagged with the iframe's DOM id.
_INTENT_LISTENER_JS = """
if (!window.__runbookDagIntent) {
  window.__runbookDagIntent = true;
  window.addEventListener("message", (e) => {
    const m = e.data;
    if (!m || typeof m !== "object" || m.type !== "intent") return;
    const frame = Array.from(document.querySelectorAll("iframe")).find((f) => f.contentWindow === e.source);
    if (!frame) return;
    emitEvent("%s", { frame: frame.id, op: m.op, no: m.no, key: m.key, value: m.value });
  });
}
""" % _INTENT_EVENT


def _viewer_file(name: str) -> Path:
    path = VIEWER_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"runbook viewer file not found: {path}")
    return path


def _page(name: str) -> str:
    """The viewer html with its ``<script src>`` files inlined."""
    html = _viewer_file(name).read_text(encoding="utf-8")

    def inline(m: re.Match[str]) -> str:
        js = _viewer_file(m.group(1)).read_text(encoding="utf-8")
        return "<script>\n" + js.replace("</script", "<\\/script") + "\n</script>"

    return _SCRIPT_SRC.sub(inline, html)


def normalize_viewer_model(model: dict[str, Any]) -> dict[str, Any]:
    if model.get("stage") == "pin":
        return {**model, "stage": "pinned"}
    return model


class _FrameViewer:
    """Iframe viewer driven with `frame.client.run_javascript` (safe across parent `.clear()`)."""

    page = ""

    def __init__(self, *, min_height: str = "420px") -> None:
        _viewer_file(self.page)  # fail when the tab is built, not later with a blank iframe
        self.theme = "tui"
        self.model: dict[str, Any] | None = None
        self._mounted_doc = False
        self._seq = 0
        self.frame = (
            ui.element("iframe")
            .classes("w-full flex-1 min-h-0 border-0")
            .style(f"min-height: {min_height}")
        )

    def _prepare(self, model: dict[str, Any]) -> dict[str, Any]:
        return model

    def _run_js(self, code: str) -> None:
        client = getattr(self.frame, "client", None)
        if client is not None:
            client.run_javascript(code)

    def _post(self, msg: dict[str, Any]) -> None:
        data = json.dumps(msg, ensure_ascii=False)
        self._run_js(
            f"const el = getHtmlElement({self.frame.id});"
            f"if (el && el.contentWindow) el.contentWindow.postMessage({data}, '*');"
        )

    def _build_srcdoc(self, model: dict[str, Any] | None) -> str:
        data = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
        html = _page(self.page).replace('data-theme="doc"', f'data-theme="{self.theme}"', 1)
        return html.replace(_MODEL_MARK, data, 1)

    def show(self, model: dict[str, Any]) -> None:
        norm = self._prepare(model)
        self.model = norm
        self._seq += 1
        self.frame._props["srcdoc"] = self._build_srcdoc(norm)
        if not self._mounted_doc:
            self.frame.update()
            self._mounted_doc = True
            return
        self._post({"type": "model", "seq": self._seq, "payload": norm})

    def toggle_theme(self) -> None:
        self.theme = "doc" if self.theme == "tui" else "tui"
        self._seq += 1
        if self.model is not None:
            self.frame._props["srcdoc"] = self._build_srcdoc(self.model)
        self._post({"type": "theme", "seq": self._seq, "payload": self.theme})


class DocViewer(_FrameViewer):
    """Doc viewer (former `catalog.RunbookViewer`): same behaviour, html/js in `frame/viewer/`."""

    page = "doc.html"

    def _prepare(self, model: dict[str, Any]) -> dict[str, Any]:
        return normalize_viewer_model(model)

    def print(self) -> None:
        self._run_js(
            f"const el = getHtmlElement({self.frame.id});"
            f"if (el && el.contentWindow) el.contentWindow.print();"
        )


RunbookViewer = DocViewer


class DagViewer(_FrameViewer):
    """DAG viewer: draft wiring as a graph; the only edit is a slot source (dropdown inside the viewer).

    `on_bind(no, key, src)` applies the edit (e.g. `runbook.set_binding`) and returns the new DAG model,
    or None after calling `show()` itself. If it raises, a notification is shown and the current model is
    sent back, so the dropdown returns to its previous value.
    """

    page = "dag.html"

    def __init__(
        self,
        *,
        on_bind: Callable[[str, str, str], dict[str, Any] | None],
        min_height: str = "360px",
    ) -> None:
        super().__init__(min_height=min_height)
        self._on_bind = on_bind
        self._run_js(_INTENT_LISTENER_JS)  # queued until the client is connected
        ui.on(_INTENT_EVENT, self._handle_intent)

    def _offered(self, no: str, key: str, src: str) -> bool:
        """Whether `src` was one of the options the viewer showed for this slot ("" = unbind)."""
        if src == "":
            return True
        for n in (self.model or {}).get("nodes", []):
            if str(n.get("no")) != no:
                continue
            for s in n.get("slots", []):
                if s.get("key") == key:
                    return src == s.get("src") or any(str(o[0]) == src for o in s.get("options", []))
        return False

    def _dom_id(self) -> str:
        # The DOM id prefix differs between NiceGUI versions ("c5", "ng-5").
        return str(getattr(self.frame, "html_id", f"c{self.frame.id}"))

    def _handle_intent(self, e: Any) -> None:
        data = e.args if isinstance(e.args, dict) else {}
        if data.get("frame") != self._dom_id() or data.get("op") != "set_binding":
            return
        no, key, src = str(data.get("no") or ""), str(data.get("key") or ""), str(data.get("value") or "")
        new_model: dict[str, Any] | None = None
        try:
            if not self._offered(no, key, src):
                raise ValueError(f"'{src}' is not a source offered for {no}.{key}")
            new_model = self._on_bind(no, key, src)
        except Exception as exc:  # the console decides; the viewer only shows the result
            ui.notify(f"[ERR] {no}.{key}: {exc}", type="negative")
        current = new_model if new_model is not None else self.model
        if current is not None:
            self.show(current)


def source_label(raw_key: str, node_names: Mapping[str, str]) -> str:
    """'$input.x' -> '$input · x', '02.items' -> '02 transform.jq · items' (same as draft.py)."""
    if raw_key.startswith("$input."):
        return f"$input · {raw_key.removeprefix('$input.')}"
    head, sep, tail = raw_key.partition(".")
    if sep and head in node_names:
        return f"{head} {node_names[head]} · {tail}"
    return raw_key


def build_dag_model(
    draft: Mapping[str, Any],
    available_keys: Callable[[str], Sequence[str]],
    *,
    editable: bool = True,
) -> dict[str, Any]:
    """DAG viewer model from `runbook.draft` and `runbook.available_keys` (passed in; no bridge import here)."""
    node_names = {n["no"]: n.get("name", "") for n in draft.get("nodes", [])}
    nodes = []
    for n in draft.get("nodes", []):
        avail = list(available_keys(n["no"]))
        slots = []
        for key, src in (n.get("in") or {}).items():
            src = src or ""
            keys = avail + ([src] if src and src not in avail else [])
            slots.append({"key": key, "src": src, "options": [[k, source_label(k, node_names)] for k in keys]})
        nodes.append({"no": n["no"], "name": n.get("name", ""), "kind": n.get("kind", ""), "slots": slots})
    inputs = [
        {"key": k, "shape": spec.get("shape", ""), "mode": spec.get("mode", ""),
         **({"value": spec["value"]} if "value" in spec else {})}
        for k, spec in (draft.get("inputs") or {}).items()
    ]
    return {"flow": draft.get("flow"), "version": draft.get("version"), "editable": editable,
            "inputs": inputs, "nodes": nodes}