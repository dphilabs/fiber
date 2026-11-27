# fiber.studio.console.bridge.runbook
from __future__ import annotations

import copy
import json
import time
from typing import Any

from fiber.studio.harness.reflex.context import FlowCatalog

_TEMPLATES = ("quote_to_sheet", "price_watch", "mail_to_ledger")
_SAMPLES = ("quote_sample.xlsx", "quote_empty.xlsx")

# Source key -> shape (for mock type checks)
_SHAPES = {
    "$input.quote_file": "file", "$input.sheet_id": "text",
    "01.rows": "obj[]", "02.items": "obj[]", "02.changed": "bool", "02.text": "text", "04.text": "text",
    "05.count": "num", "05.url": "text",
}

_DRAFT_SEED: dict[str, Any] = {
    "flow": "quote_to_sheet",
    "version": 3,
    "nodes": [
        {"no": "01", "name": "sheet.read", "kind": "py", "in": {"file": "$input.quote_file"}, "want": {"file": "file"},
         "cfg": {"sheet": "Sheet1"}, "out": ["rows"]},
        {"no": "02", "name": "transform.jq", "kind": "exec", "in": {"doc": "01.rows"}, "want": {"doc": "obj[]"},
         "cfg": {}, "out": ["items", "changed", "text"]},
        {"no": "03", "name": "gate(changed)", "kind": "gate", "in": {"changed": "02.changed"}, "want": {"changed": "bool"},
         "cfg": {}, "out": []},
        {"no": "04", "name": "llm.summarize", "kind": "llm", "in": {"text": "02.items"}, "want": {"text": "text"},
         "cfg": {"model": "gemini-3.1-flash"}, "out": ["text"]},
        {"no": "05", "name": "sheet.write", "kind": "mcp", "in": {"rows": "", "sheet_id": "$input.sheet_id"},
         "want": {"rows": "obj[]", "sheet_id": "text"}, "cfg": {}, "out": ["count", "url"]},
    ],
    "inputs": {"quote_file": {"shape": "file", "mode": "runtime"},
               "sheet_id": {"shape": "text", "mode": "fixed", "value": "1AbX..."}},
    "outputs": {"rows_written": "05.count", "sheet_url": "05.url"},
}

_RUN_SEED: dict[str, list[dict[str, Any]]] = {
    "quote_to_sheet": [
        {"no": 43, "at": "11-27 09:00", "by": "cron", "status": "paused", "note": "05 sheet.write · approve 36 rows"},
        {"no": 42, "at": "11-26 09:00", "by": "cron", "status": "ok", "note": "4.2s · rows_written=36"},
        {"no": 41, "at": "11-25 09:00", "by": "cron", "status": "fail", "note": "01 sheet.read · header missing"},
    ],
    "price_watch": [
        {"no": 18, "at": "11-27 08:30", "by": "cron", "status": "fail", "note": "01 stealth_fetch · timeout"},
    ],
    "mail_to_ledger": [],
}


class RunbookBridge:
    """MOCK bridge. Action names follow theoria.abc.console.runbook.bridge §3."""

    def __init__(self) -> None:
        self.repo = FlowCatalog.from_env()
        self.origin: str | None = None  # repo template the draft came from
        self.draft: dict[str, Any] = copy.deepcopy(_DRAFT_SEED)
        self.dirty = False
        self.pins: dict[str, list[int]] = {"quote_to_sheet": [1, 2, 3], "price_watch": [1], "mail_to_ledger": [1, 2]}
        self.runs = copy.deepcopy(_RUN_SEED)
        self.triggers: dict[str, dict[str, Any]] = {
            "quote_to_sheet": {"kind": "cron", "cron": "0 9 * * 1-5", "tz": "Asia/Seoul", "on": True,
                               "map": {"quote_file": "source: drive:/quotes/latest", "sheet_id": "(fixed)"}},
            "price_watch": {"kind": "cron", "cron": "30 8 * * *", "tz": "Asia/Seoul", "on": True, "map": {}},
            "mail_to_ledger": {"kind": "event", "cron": "", "tz": "Asia/Seoul", "on": False,
                               "map": {"query": "payload.query"}},
        }

    def list_active(self) -> list[dict[str, Any]]:
        rows = []
        for flow, versions in self.pins.items():
            last = (self.runs.get(flow) or [None])[0]
            rows.append({
                "flow": flow, "version": versions[-1], "trigger": self.triggers[flow]["kind"],
                "status": last["status"] if last else "none", "note": last["note"] if last else "",
                "stale": flow == "mail_to_ledger",
            })
        return rows

    def list_catalog(self) -> list[dict[str, Any]]:
        """Repo flows + pinned runbooks, one row per flow name. `src` False = pinned but not in the repo index."""
        repo = {n: desc for n, _, desc in self.repo.flows()} if self.repo.enabled else {}
        active = {r["flow"]: r for r in self.list_active()}
        rows = []
        for flow in sorted(set(repo) | set(active)):
            a = active.get(flow)
            rows.append({
                "flow": flow, "desc": repo.get(flow, ""), "src": flow in repo or not self.repo.enabled,
                "seeds": len(self.repo.samples(flow)), "fixture": bool(self.repo.mock_outputs(flow)),
                "pin": a["version"] if a else None, "trigger": a["trigger"] if a else "",
                "status": a["status"] if a else "", "note": a["note"] if a else "",
            })
        return rows

    def list_versions(self, flow: str) -> list[int]:
        return list(self.pins.get(flow, []))

    def list_templates(self) -> list[dict[str, Any]]:
        if self.repo.enabled:
            return [{"name": n, "desc": desc} for n, _, desc in self.repo.flows()]
        return [{"name": t, "desc": ""} for t in _TEMPLATES]

    def list_samples(self, flow: str) -> list[str]:
        names = list(self.repo.samples(flow))
        return names or list(_SAMPLES)

    def repo_status(self) -> tuple[str, str]:
        if not self.repo.enabled:
            return ("[MOCK · REPO ERR]", "red") if self.repo.notes else ("[MOCK]", "amber")
        n, err = len(self.repo.flows()), len(self.repo.notes)
        return (f"[REPO {n} · {err} ERR]", "red") if err else (f"[REPO {n}]", "green")

    def available_keys(self, step_no: str) -> list[str]:
        keys = [f"$input.{k}" for k in self.draft["inputs"]]
        for n in self.draft["nodes"]:
            if n["no"] >= step_no:
                break
            keys += [f"{n['no']}.{o}" for o in n["out"]]
        return keys

    def list_runs(self, flow: str) -> list[dict[str, Any]]:
        return list(self.runs.get(flow, []))

    def next_run(self, flow: str) -> str:
        t = self.triggers[flow]
        return "11-28 09:00" if t["kind"] == "cron" and t["on"] else "-"

    # --- compile (mock) --------------------------------------------------------

    def _compile(self) -> dict[str, Any]:
        unbound, mismatch = [], []
        for n in self.draft["nodes"]:
            for key, src in n["in"].items():
                if not src:
                    unbound.append(f"{n['no']}.{key}")
                elif _SHAPES.get(src) != n["want"].get(key):
                    mismatch.append(f"{n['no']}.{key}")
        return {"ok": not (unbound or mismatch), "unbound": unbound, "mismatch": mismatch}

    def _node_status(self, n: dict[str, Any], comp: dict[str, Any]) -> str:
        ids = {f"{n['no']}.{k}" for k in n["in"]}
        if ids & set(comp["unbound"]):
            return "fail"
        if ids & set(comp["mismatch"]):
            return "wait"
        return "ok"

    def _origin_blocks(self, name: str | None) -> list[dict[str, Any]]:
        f = self.repo.flow(name) if name else None
        if f is None:
            return []
        _, spec, desc = f
        return [*([{"kind": "paragraph", "text": desc}] if desc else []),
                {"kind": "code", "title": "flow", "lang": "reflex", "text": spec}]

    def template_model(self, name: str) -> dict[str, Any]:
        if self.repo.flow(name) is None:
            return {"flow": name, "stage": "template", "title": f"{name} · template",
                    "blocks": [{"kind": "callout", "tone": "fail", "text": "template not found in reflexor index"}]}
        blocks: list[dict[str, Any]] = [{"kind": "callout", "tone": "ok", "text": "TEMPLATE · reflexor"},
                                        *self._origin_blocks(name)]
        if self.repo.notes:
            blocks.append({"kind": "table", "title": "notes", "headers": ["note"], "rows": [[n] for n in self.repo.notes]})
        seeds = self.repo.samples(name)
        if seeds:
            blocks.append({"kind": "table", "title": "seed", "headers": ["sample", "key", "value"],
                           "rows": [[s, k, _short(v)] for s, kv in seeds.items() for k, v in kv.items()]})
        mocks = self.repo.mock_outputs(name)
        if mocks:
            blocks.append({"kind": "table", "title": "mock_outputs", "headers": ["unit", "output"],
                           "rows": [[u, _short(v)] for u, v in mocks.items()]})
        return {"flow": name, "version": None, "stage": "template", "title": f"{name} · template", "blocks": blocks}

    def project(self, flow: str, version: int | None = None) -> dict[str, Any]:
        """Draft model for the current draft; pinned model (with last run) for other flows/versions."""
        if flow != self.draft["flow"] or (version is not None and version != self.draft["version"]):
            return self.run_model(flow, version=version)
        d, comp = self.draft, self._compile()
        compile_text = (
            "COMPILE OK · bindings all set" if comp["ok"]
            else f"COMPILE FAIL · {len(comp['unbound'])} unbound · {len(comp['mismatch'])} type mismatch"
        )
        steps = [{
            "label": f"{n['no']} {n['name']}",
            "detail": " · ".join(f"{k} <- {v or '(unbound)'}" for k, v in n["in"].items()),
            "status": self._node_status(n, comp),
        } for n in d["nodes"]]
        return {
            "flow": d["flow"], "version": d["version"], "stage": "draft",
            "title": f"{d['flow']} · Draft v{d['version']}" + (" [*]" if self.dirty else ""),
            "blocks": [
                {"kind": "callout", "tone": "ok" if comp["ok"] else "fail", "text": compile_text},
                *self._origin_blocks(self.origin),
                {"kind": "flow", "title": "nodes (mock until S2)" if self.origin else "", "steps": steps},
                {"kind": "table", "title": "$input", "headers": ["key", "shape", "mode"],
                 "rows": [[k, v["shape"], v["mode"]] for k, v in d["inputs"].items()]},
                {"kind": "table", "title": "$out", "headers": ["key", "from", "show"],
                 "rows": [[k, v, "show"] for k, v in d["outputs"].items()]},
            ],
        }

    def run_model(self, flow: str, *, version: int | None = None, run_no: int | None = None) -> dict[str, Any]:
        ver = version or (self.pins.get(flow) or [1])[-1]
        runs = self.runs.get(flow, [])
        run = next((r for r in runs if r["no"] == run_no), runs[0] if runs else None)
        blocks: list[dict[str, Any]] = []
        if run is None:
            blocks.append({"kind": "callout", "tone": "none", "text": "No runs yet. Press [RUN NOW]."})
        else:
            tone = {"paused": "dry", "ok": "ok", "fail": "fail"}.get(run["status"], "run")
            blocks.append({"kind": "callout", "tone": tone, "text": f"{run['status'].upper()} · {run['note']}"})
            events = [
                {"node": "01 sheet.read", "status": "fail" if run["status"] == "fail" else "ok", "dur": "0.4s"},
                {"node": "02 transform.jq", "status": "none" if run["status"] == "fail" else "ok", "dur": "0.1s"},
                {"node": "03 gate(changed)", "status": "none" if run["status"] == "fail" else "ok", "dur": ""},
                {"node": "preflight", "status": "none" if run["status"] == "fail" else "ok",
                 "dur": "", "note": "policy auto-approve"},
                {"node": "05 sheet.write", "status": {"paused": "wait", "ok": "ok"}.get(run["status"], "none"),
                 "dur": "", "note": "approval pending" if run["status"] == "paused" else ""},
            ]
            blocks.append({"kind": "timeline", "events": events})
        blocks.append({"kind": "table", "title": "runs", "headers": ["#", "at", "by", "status", "note"],
                       "rows": [[f"#{r['no']}", r["at"], r["by"], r["status"].upper(), r["note"]] for r in runs]})
        title = f"{flow} · v{ver}" + (f" · Run #{run['no']} · {run['by']} · {run['at']}" if run else "")
        return {"flow": flow, "version": ver, "stage": "run", "title": title, "blocks": blocks}

    # --- 3.2 draft edit ---------------------------------------------------------

    def new_draft(self, *, from_template: str | None = None, from_version: int | None = None) -> dict[str, Any]:
        if from_template is not None:
            self.origin = from_template if self.repo.flow(from_template) else None
        self.draft = copy.deepcopy(_DRAFT_SEED)
        self.draft["flow"] = from_template or self.draft["flow"]
        self.draft["version"] = (from_version or 0) + 1 if from_version else 1
        self.dirty = True
        return self.project(self.draft["flow"])

    def save_draft(self) -> None:
        self.dirty = False

    def import_package(self, raw: str) -> dict[str, Any]:
        data = json.loads(raw)
        self.triggers.setdefault(data.get("flow", "imported"), {"kind": "manual", "cron": "", "tz": "UTC", "on": False, "map": {}})
        return {"flow": data.get("flow", "imported"), "compile": "OK", "stale_units": 1, "triggers": "OFF"}

    def set_binding(self, step_no: str, key: str, source: str) -> None:
        node = next(n for n in self.draft["nodes"] if n["no"] == step_no)
        node["in"][key] = source
        self.dirty = True

    def patch(self, step_no: str, *, cfg: dict[str, Any]) -> None:
        node = next(n for n in self.draft["nodes"] if n["no"] == step_no)
        node["cfg"].update(cfg)
        self.dirty = True

    def configure_manifest(self, key: str, mode: str) -> None:
        self.draft["inputs"][key]["mode"] = mode
        self.dirty = True

    def dryrun(self, sample: str, sources: dict[str, str]) -> dict[str, Any]:
        comp = self._compile()
        events, halted = [], False
        notes = {"01": "38 rows", "02": "38 -> 36", "03": "pass", "04": "1.2k tok · guard masked", "05": "+36 rows (not written)"}
        for n in self.draft["nodes"]:
            src = sources.get(n["no"], "LIVE")
            if halted:
                events.append({"node": f"{n['no']} {n['name']}", "status": "none", "tag": src, "note": "skipped"})
                continue
            st = self._node_status(n, comp)
            if st == "fail":
                halted = True
            status = "fail" if st == "fail" else ("dry" if src == "DRY" else "ok")
            events.append({"node": f"{n['no']} {n['name']}", "status": status, "tag": src,
                           "dur": "0.4s" if n["no"] == "01" else "", "note": "unbound input" if st == "fail" else notes[n["no"]]})
        write = self.draft["nodes"][-1]
        flow = self.draft["flow"]
        fixture: list[dict[str, Any]] = []
        seed = self.repo.samples(flow).get(sample)
        if seed is not None:
            fixture.append({"kind": "table", "title": f"seed · {sample}", "headers": ["key", "value"],
                            "rows": [[k, _short(v)] for k, v in seed.items()]})
        mocks = self.repo.mock_outputs(flow)
        if mocks:
            fixture.append({"kind": "table", "title": "FIXTURE · mock_outputs", "headers": ["unit", "output"],
                            "rows": [[u, _short(v)] for u, v in mocks.items()]})
        return {
            "flow": self.draft["flow"], "version": self.draft["version"], "stage": "dryrun",
            "title": f"{self.draft['flow']} · Draft v{self.draft['version']} · dry run · {sample}",
            "blocks": [
                {"kind": "callout", "tone": "ok" if comp["ok"] else "fail",
                 "text": "COMPILE OK" if comp["ok"] else "COMPILE FAIL · fix bindings before dry run"},
                {"kind": "timeline", "events": events},
                {"kind": "table", "title": "node 05 · binding resolution", "headers": ["in", "source", "value"],
                 "rows": [[k, v or "(unbound)", "[ {item, price, ...} x36 ]" if k == "rows" and v else ('"1AbX..."' if v else "-")]
                          for k, v in write["in"].items()]},
                {"kind": "table", "title": "$out", "headers": ["key", "value"],
                 "rows": [["rows_written", "36" if comp["ok"] else "-"], ["sheet_url", "https://..." if comp["ok"] else "-"]]},
                *fixture,
            ],
        }

    def pin(self) -> dict[str, Any]:
        comp = self._compile()
        if not comp["ok"]:
            raise ValueError(f"compile failed: unbound={comp['unbound']} mismatch={comp['mismatch']}")
        flow, prev = self.draft["flow"], self.draft["version"]
        nxt = max(self.pins.get(flow, [0])) + 1
        return {"flow": flow, "version": nxt, "summary": f"v{prev} -> v{nxt} · bindings updated", "keep": f"v{prev} kept (rollback)"}

    def confirm_pin(self, receipt: dict[str, Any]) -> None:
        self.pins.setdefault(receipt["flow"], []).append(receipt["version"])
        self.draft["version"] = receipt["version"]
        self.dirty = False

    def export_package(self, flow: str, version: int) -> bytes:
        pkg = {"flow": flow, "version": version, "exported_at": time.time(), "flow_dsl": self._dsl(),
               "manifest": {"inputs": self.draft["inputs"], "outputs": self.draft["outputs"]},
               "triggers": [{**self.triggers.get(flow, {}), "on": False}], "auth": ["google:sheets"]}
        return json.dumps(pkg, indent=2, ensure_ascii=False).encode("utf-8")

    def _dsl(self) -> str:
        f = self.repo.flow(self.origin) if self.origin else None
        return f[1] if f is not None else "(mock)"

    # --- 3.4 operate -----------------------------------------------------------

    def configure_trigger(self, flow: str, **spec: Any) -> None:
        self.triggers[flow].update(spec)

    def fire(self, flow: str) -> int:
        runs = self.runs.setdefault(flow, [])
        no = (runs[0]["no"] + 1) if runs else 1
        runs.insert(0, {"no": no, "at": time.strftime("%m-%d %H:%M"), "by": "manual", "status": "paused",
                        "note": "05 sheet.write · approve 36 rows"})
        return no

    def resume(self, flow: str, run_no: int) -> None:
        for r in self.runs.get(flow, []):
            if r["no"] == run_no and r["status"] == "paused":
                r["status"], r["note"] = "ok", "approved · rows_written=36"

def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."

runbook = RunbookBridge()