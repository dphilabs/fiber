/* fiber.studio.runbook.viewer · Doc viewer script (moved unchanged from ui/component/viewer.html). */
(function () {
  const STAGES = ["template", "draft", "dryrun", "pinned", "run"];
  const STAGE_LABEL = {
    template: "TEMPLATE",
    draft: "DRAFT",
    dryrun: "DRY RUN",
    pinned: "PINNED",
    run: "TRIGGER",
  };
  const STAGE_ALIAS = { pin: "pinned", trigger: "run" };

  const root = document.getElementById("root");
  let currentModel = null;
  let lastSeq = -1;
  let lastReport = { flow: null, version: null, blocks: 0, errors: [] };

  // Apply `?theme=tui|doc` when opened standalone via URL
  const urlTheme = new URLSearchParams(window.location.search).get("theme");
  if (urlTheme === "tui" || urlTheme === "doc") {
    document.documentElement.setAttribute("data-theme", urlTheme);
  }

  function esc(v) {
    const d = document.createElement("div");
    d.textContent = v == null ? "" : String(v);
    return d.innerHTML;
  }

  function st(status) {
    const norm = String(status || "none").toLowerCase();
    return `<span class="st st-${esc(norm)}">[${esc(norm.toUpperCase())}]</span>`;
  }

  function splitStepLabel(rawLabel, fallbackIdx) {
    const text = String(rawLabel || "").trim();
    const m = /^(\d{2,3})\s+(.+)$/.exec(text);
    if (m) return { no: m[1], name: m[2] };
    return { no: String(fallbackIdx + 1).padStart(2, "0"), name: text };
  }

  const KINDS = {
    paragraph: (b) => `<p class="para">${esc(b.text)}</p>`,
    callout: (b) => `<div class="callout t-${esc(b.tone || "run")}">${esc(b.text)}</div>`,
    table: (b) => {
      const headers = b.headers || [];
      const rows = b.rows || [];
      const headHtml = headers.length
        ? `<thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>`
        : "";
      const bodyHtml = rows.length
        ? rows.map((r) => `<tr>${(Array.isArray(r) ? r : [r]).map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")
        : `<tr><td class="empty-cell" colspan="${Math.max(1, headers.length)}">(empty)</td></tr>`;
      return `<div class="table-wrap"><table>${headHtml}<tbody>${bodyHtml}</tbody></table></div>`;
    },
    flow: (b) => `<ol class="flow">${(b.steps || []).map((s, i) => {
      const sp = splitStepLabel(s.label, i);
      const no = s.no ? String(s.no) : sp.no;
      const lbl = s.no ? String(s.label || "") : sp.name;
      return `<li><span class="num">${esc(no)}</span><span class="label">${esc(lbl)}</span>${st(s.status)}` +
        `<span class="detail">${esc(s.detail || "")}</span></li>`;
    }).join("")}</ol>`,
    timeline: (b) => `<ol class="timeline">${(b.events || []).map((e) => {
      const sp = splitStepLabel(e.node, 0);
      const hasLeadingNo = /^\d{2,3}\s+/.test(String(e.node || "").trim());
      const numCol = hasLeadingNo ? `<span class="num">${esc(sp.no)}</span>` : "";
      const nodeLbl = hasLeadingNo ? sp.name : String(e.node || "");
      return `<li>${numCol}<span class="label">${esc(nodeLbl)}</span>${st(e.status)}` +
        (e.tag ? `<span class="tag">${esc(e.tag)}</span>` : "") +
        `<span class="note">${esc([e.dur, e.note].filter(Boolean).join(" · "))}</span></li>`;
    }).join("")}</ol>`,
    code: (b) => `<pre class="code" data-lang="${esc(b.lang || "")}">${esc(b.text)}</pre>`,
    compare: (b) => {
      const left = b.left || { heading: "before", body: "" };
      const right = b.right || { heading: "after", body: "" };
      return `<div class="compare"><div><b>${esc(left.heading)}</b>${esc(left.body)}</div>` +
        `<div><b>${esc(right.heading)}</b>${esc(right.body)}</div></div>`;
    },
  };

  function stagesHtml(rawStage) {
    const stage = STAGE_ALIAS[rawStage] || rawStage || "";
    const idx = STAGES.indexOf(stage);
    return STAGES.map((s, i) => {
      const cls = idx >= 0 ? (i < idx ? "done" : i === idx ? "cur" : "") : "";
      const sep = i < STAGES.length - 1
        ? `<span class="sep">${s === "pinned" ? "&#9655;" : "&#9654;"}</span>`
        : "";
      return `<span class="${cls}">${STAGE_LABEL[s]}</span>${sep}`;
    }).join("");
  }

  function notifyReport() {
    if (window.parent && window.parent !== window) {
      try {
        window.parent.postMessage(
          { type: "report", flow: lastReport.flow, version: lastReport.version, seq: lastSeq, payload: lastReport },
          "*"
        );
      } catch (_) {}
    }
  }

  function render(model) {
    const errors = [];
    if (!model || typeof model !== "object") {
      currentModel = null;
      root.innerHTML = `<div class="empty">no model</div>`;
      lastReport = { flow: null, version: null, blocks: 0, errors: [] };
      return lastReport;
    }
    currentModel = JSON.parse(JSON.stringify(model));
    const blocks = (currentModel.blocks || []).map((b, i) => {
      if (!b || !KINDS[b.kind]) {
        errors.push(`unknown kind '${b && b.kind}' at ${i}`);
        return "";
      }
      const title = b.title ? `<div class="block-title">${esc(b.title)}</div>` : "";
      return `<section class="block" data-idx="${i}">${title}${KINDS[b.kind](b)}</section>`;
    }).join("");
    const bodyHtml = blocks || `<div class="empty">(no blocks)</div>`;
    root.innerHTML =
      `<h1>${esc(currentModel.title || currentModel.flow || "runbook")}</h1>` +
      `<div class="stages">${stagesHtml(currentModel.stage)}</div>` +
      bodyHtml;
    lastReport = {
      flow: currentModel.flow || null,
      version: currentModel.version ?? null,
      blocks: (currentModel.blocks || []).length,
      errors,
    };
    return lastReport;
  }

  function patch(partial) {
    if (!partial || typeof partial !== "object") return lastReport;
    if (!currentModel) return render(partial);
    const next = Object.assign({}, currentModel, partial);
    if (Array.isArray(partial.blockPatches) && Array.isArray(currentModel.blocks)) {
      const cloned = currentModel.blocks.slice();
      for (const p of partial.blockPatches) {
        if (p && typeof p.index === "number" && p.block) cloned[p.index] = p.block;
      }
      next.blocks = cloned;
      delete next.blockPatches;
    }
    return render(next);
  }

  function applyEvent(ev) {
    if (!currentModel || !ev) return lastReport;
    const blocks = Array.isArray(currentModel.blocks) ? currentModel.blocks.slice() : [];
    let tlIdx = blocks.findIndex((b) => b && b.kind === "timeline");
    if (tlIdx < 0) {
      blocks.push({ kind: "timeline", title: "trace", events: [ev] });
    } else {
      const evs = Array.isArray(blocks[tlIdx].events) ? blocks[tlIdx].events.slice() : [];
      const pos = evs.findIndex((x) => x && x.node === ev.node);
      if (pos >= 0) evs[pos] = Object.assign({}, evs[pos], ev);
      else evs.push(ev);
      blocks[tlIdx] = Object.assign({}, blocks[tlIdx], { events: evs });
    }
    return render(Object.assign({}, currentModel, { blocks }));
  }

  function setTheme(theme) {
    if (theme === "tui" || theme === "doc") {
      document.documentElement.setAttribute("data-theme", theme);
    }
  }

  window.__VIEWER__ = { render, patch, applyEvent, setTheme, report: () => lastReport };

  window.addEventListener("message", (e) => {
    const m = e.data;
    if (!m || typeof m !== "object" || !m.type) return;
    if (typeof m.seq === "number") {
      if (m.type !== "model" && m.seq <= lastSeq) return;
      lastSeq = m.seq;
    }
    if (m.type === "model") {
      render(m.payload);
      notifyReport();
    } else if (m.type === "patch") {
      patch(m.payload);
      notifyReport();
    } else if (m.type === "event") {
      applyEvent(m.payload);
    } else if (m.type === "theme") {
      setTheme(m.payload);
    }
  });

  try {
    const raw = document.getElementById("model-data").textContent;
    render(JSON.parse(raw));
  } catch (err) {
    lastReport = { flow: null, version: null, blocks: 0, errors: [String(err)] };
  }
})();