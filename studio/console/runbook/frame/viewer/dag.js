/* fiber.studio.runbook.viewer · DAG viewer script.
 *
 * model: {
 *   flow, version?, editable?,                       // editable defaults to true
 *   inputs: [{key, shape, mode, value?}],
 *   nodes:  [{no, name, kind?, slots: [{key, src, options: [[value, label], ...]}]}]
 * }
 * src: "$input.<key>" or "<no>.<port>"; "" = unbound. Edges come from slots[].src.
 * Cards are HTML (so a slot can hold a dropdown); dagre only computes their positions and the edge points.
 * A dropdown change posts {type: "intent", op: "set_binding", no, key, value} to the parent and stays
 * disabled until the console answers with the next model.
 */
(function () {
  "use strict";

  const MODEL_MARK = "/*__" + "MODEL__*/null";
  const INPUT = "$input";

  const root = document.getElementById("root");
  let model = null;
  let lastSeq = -1;

  function esc(v) {
    const d = document.createElement("div");
    d.textContent = v == null ? "" : String(v);
    return d.innerHTML;
  }

  function escAttr(v) {
    return esc(v).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function srcNode(src) {
    const s = String(src || "");
    if (!s) return "";
    if (s.startsWith(INPUT + ".")) return INPUT;
    const i = s.indexOf(".");
    return i > 0 ? s.slice(0, i) : "";
  }

  function optionLabel(slot) {
    const hit = (slot.options || []).find((o) => String(o[0]) === slot.src);
    return hit ? String(hit[1] ?? hit[0]) : String(slot.src || "");
  }

  // ---- cards ---------------------------------------------------------------

  function inputCard(inputs) {
    const rows = inputs.map((it) => {
      const fixed = it.mode === "fixed" && it.value !== undefined ? ` = ${JSON.stringify(it.value)}` : "";
      return `<div class="row"><span class="key">${esc(it.key)}</span>` +
        `<span class="val">${esc(it.shape || "")}${esc(fixed)} · ${esc(it.mode || "")}</span></div>`;
    }).join("");
    return `<div class="card" data-node="${escAttr(INPUT)}">` +
      `<div class="card-head"><span class="name">$input</span></div>` +
      (rows || `<div class="row"><span class="val">(no inputs)</span></div>`) + `</div>`;
  }

  function slotRow(n, s, editable) {
    const src = String(s.src || "");
    const id = `${n.no}:${s.key}`;
    let ctrl;
    if (editable) {
      const opts = (s.options || []).map((o) => [String(o[0]), String(o[1] ?? o[0])]);
      if (src && !opts.some(([v]) => v === src)) opts.push([src, src]);
      ctrl = `<select data-no="${escAttr(n.no)}" data-key="${escAttr(s.key)}">` +
        `<option value=""${src ? "" : " selected"}>(select source...)</option>` +
        opts.map(([v, l]) => `<option value="${escAttr(v)}"${v === src ? " selected" : ""}>${esc(l)}</option>`).join("") +
        `</select>`;
    } else {
      ctrl = `<span class="val">${esc(src ? optionLabel(s) : "(unbound)")}</span>`;
    }
    return `<div class="row slot${src ? "" : " unbound"}" data-slot="${escAttr(id)}" data-src="${escAttr(srcNode(src))}">` +
      `<span class="key">${esc(s.key)} ←</span>${ctrl}<span class="dot">${src ? "●" : "○"}</span></div>`;
  }

  function nodeCard(n, editable) {
    const slots = n.slots || [];
    const unbound = slots.some((s) => !s.src);
    return `<div class="card${unbound ? " unbound" : ""}" data-node="${escAttr(n.no)}">` +
      `<div class="card-head"><span class="no">${esc(n.no)}</span><span class="name">${esc(n.name || "")}</span></div>` +
      slots.map((s) => slotRow(n, s, editable)).join("") + `</div>`;
  }

  // ---- render + layout -----------------------------------------------------

  function render(next) {
    if (!next || typeof next !== "object") {
      model = null;
      root.innerHTML = `<div class="empty">no model</div>`;
      return;
    }
    model = next;
    const nodes = (model.nodes || []).filter((n) => n && n.no != null);
    const editable = model.editable !== false;
    const unbound = nodes.reduce((k, n) => k + (n.slots || []).filter((s) => !s.src).length, 0);
    const title = `${model.flow || "runbook"}${model.version != null ? " · v" + model.version : ""}`;

    root.innerHTML =
      `<div class="head"><h1>${esc(title)}</h1>` +
      (unbound ? `<span class="badge warn">UNBOUND ${unbound}</span>` : `<span class="badge ok">bindings ok</span>`) +
      `</div>` +
      `<div class="canvas" id="canvas"><svg class="edges" id="edges"></svg>` +
      inputCard(model.inputs || []) + nodes.map((n) => nodeCard(n, editable)).join("") +
      `</div>`;
    layout();
  }

  function layout() {
    const canvas = document.getElementById("canvas");
    const svg = document.getElementById("edges");
    if (!canvas || !svg || !model) return;

    const g = new dagre.graphlib.Graph({ multigraph: true });
    g.setGraph({ rankdir: "TB", nodesep: 24, ranksep: 44, marginx: 2, marginy: 2 });
    g.setDefaultEdgeLabel(() => ({}));

    const cards = {};
    canvas.querySelectorAll(".card").forEach((c) => {
      cards[c.dataset.node] = c;
      g.setNode(c.dataset.node, { width: c.offsetWidth, height: c.offsetHeight });
    });
    canvas.querySelectorAll(".slot").forEach((row) => {
      const from = row.dataset.src;
      const to = row.closest(".card").dataset.node;
      if (from && g.hasNode(from) && from !== to) g.setEdge(from, to, { slot: row.dataset.slot }, row.dataset.slot);
    });

    dagre.layout(g);

    g.nodes().forEach((id) => {
      const p = g.node(id);
      cards[id].style.left = `${p.x - p.width / 2}px`;
      cards[id].style.top = `${p.y - p.height / 2}px`;
    });
    const size = g.graph();
    canvas.style.width = `${Math.ceil(size.width)}px`;
    canvas.style.height = `${Math.ceil(size.height)}px`;
    svg.setAttribute("width", String(Math.ceil(size.width)));
    svg.setAttribute("height", String(Math.ceil(size.height)));

    const paths = g.edges().map((e) => {
      const pts = g.edge(e).points;
      const d = pts.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
      return `<path class="edge" data-slot="${escAttr(e.name)}" d="${d}" marker-end="url(#arrow)"/>`;
    }).join("");
    svg.innerHTML =
      `<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">` +
      `<path class="arrow" d="M0,0 L8,4 L0,8 z"/></marker></defs>` + paths;
  }

  // ---- hover: slot -> its edge and source card ----------------------------

  function highlight(row, on) {
    root.querySelectorAll(`.edge[data-slot="${CSS.escape(row.dataset.slot)}"]`).forEach((p) => p.classList.toggle("hl", on));
    const src = row.dataset.src && root.querySelector(`.card[data-node="${CSS.escape(row.dataset.src)}"]`);
    if (src) src.classList.toggle("hl", on);
  }

  root.addEventListener("mouseover", (e) => {
    const row = e.target.closest(".slot");
    if (row && !row.contains(e.relatedTarget)) highlight(row, true);
  });
  root.addEventListener("mouseout", (e) => {
    const row = e.target.closest(".slot");
    if (row && !row.contains(e.relatedTarget)) highlight(row, false);
  });

  // ---- intent: slot source -------------------------------------------------

  root.addEventListener("change", (e) => {
    const sel = e.target.closest("select[data-no]");
    if (!sel || !model || model.editable === false) return;
    sel.disabled = true;
    if (window.parent && window.parent !== window) {
      window.parent.postMessage(
        { type: "intent", op: "set_binding", no: sel.dataset.no, key: sel.dataset.key, value: sel.value },
        "*",
      );
    }
  });

  // ---- channel -------------------------------------------------------------

  function setTheme(theme) {
    if (theme === "tui" || theme === "doc") document.documentElement.setAttribute("data-theme", theme);
  }

  window.addEventListener("message", (e) => {
    if (e.source !== window.parent) return;
    const m = e.data;
    if (!m || typeof m !== "object" || !m.type) return;
    if (typeof m.seq === "number") {
      if (m.type !== "model" && m.seq <= lastSeq) return;
      lastSeq = m.seq;
    }
    if (m.type === "model") render(m.payload);
    else if (m.type === "theme") { setTheme(m.payload); layout(); }
  });

  window.__DAG__ = { render, setTheme };

  setTheme(new URLSearchParams(window.location.search).get("theme"));
  const raw = document.getElementById("model-data").textContent.trim();
  try {
    render(raw === MODEL_MARK ? null : JSON.parse(raw));
  } catch (err) {
    root.innerHTML = `<div class="empty">${esc(String(err))}</div>`;
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(layout);
})();