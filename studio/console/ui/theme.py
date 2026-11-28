# fiber.studio.console.ui.theme
from __future__ import annotations

import re
from typing import Any

from nicegui import ui

TUI_BG = "#090a0f"
TUI_SURFACE = "#0d0f17"
TUI_BLACK = "#050608"
TUI_BORDER = "#262b3d"
TUI_BORDER_ACTIVE = "#7dcfff"
TUI_FG = "#d8dee9"
TUI_MUTED = "#6c7689"

TUI_GREEN = "#50fa7b"
TUI_CYAN = "#7dcfff"
TUI_AMBER = "#e0af68"
TUI_RED = "#f7768e"

TUI_FONT_STACK = "'JetBrains Mono', 'Fira Code', 'SFMono-Regular', ui-monospace, Menlo, Monaco, Consolas, monospace"


# --- Emoji -> TUI text helpers (view layer only; state values are left untouched) ---

# Leading emoji -> bracket tag for messages (notify, labels).
_EMOJI_TAGS: dict[str, str] = {
    "💾": "SAVE",
    "🔄": "SYNC",
    "📥": "SYNC",
    "✅": "OK",
    "✓": "OK",
    "✔": "OK",
    "⚠": "WARN",
    "❌": "ERR",
    "🔴": "ERR",
    "🟢": "OK",
    "🟡": "CFG",
    "⏳": "WAIT",
    "⚡": "STBY",
    "🧩": "+",
}

# Leading emoji -> single status glyph for badges/tags.
_TAG_GLYPHS: dict[str, str] = {
    "🟢": "●",
    "✅": "●",
    "🟡": "○",
    "⏳": "~",
    "⚡": "~",
    "🔄": "~",
    "🔴": "!",
    "❌": "!",
    "⚠": "!",
    "🧩": "+",
}
_PLAIN_GLYPHS = "●○~!*+"

_EMOJI_CLASS = "\U0001F000-\U0001FAFF⌀-⏿☀-➿⬀-⯿"
_EMOJI_RE = re.compile(f"[{_EMOJI_CLASS}️‍]")
_LEAD_EMOJI_RE = re.compile(f"^\\s*([{_EMOJI_CLASS}])️?\\s*")


def strip_emoji(raw: Any) -> str:
    """Remove all emoji / pictographs, keep box-drawing and status glyphs."""
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", str(raw or ""))).strip()


def tui_text(raw: Any) -> str:
    """'💾 Saved' -> '[SAVE] Saved'; unknown emoji are dropped."""
    if raw is None:
        return ""
    s = str(raw)
    if m := _LEAD_EMOJI_RE.match(s):
        tag = _EMOJI_TAGS.get(m.group(1))
        rest = strip_emoji(s[m.end():])
        return f"[{tag}] {rest}".strip() if tag else rest
    return strip_emoji(s)


def tui_tag(raw: Any) -> str:
    """Badge formatter: '🟢 Compats: Ready (3)' -> '[● COMPATS:READY (3)]'."""
    s = str(raw or "").strip()
    if not s:
        return "[ - ]"
    if s.startswith("[") and s.endswith("]"):
        return s
    glyph = ""
    if m := _LEAD_EMOJI_RE.match(s):
        glyph = _TAG_GLYPHS.get(m.group(1), "")
        s = s[m.end():]
    elif s[0] in _PLAIN_GLYPHS:
        glyph, s = s[0], s[1:]
    s = re.sub(r"\s*:\s*", ":", strip_emoji(s)).upper()
    return f"[{glyph} {s}]" if glyph else f"[{s}]"


def tui_button_label(label: str) -> str:
    """'💾 Save & Rescan' -> '[ Save & Rescan ]'; already-bracketed labels pass through."""
    s = strip_emoji(label)
    if not s:
        m = _LEAD_EMOJI_RE.match(label or "")
        tag = _EMOJI_TAGS.get(m.group(1)) if m else None
        return f"[ {tag} ]" if tag else label
    return s if s.startswith("[") else f"[ {s} ]"


_WEB_TUI_CSS = f"""
<style id="fiber-webtui-theme">
  :root {{
    --tui-bg: {TUI_BG};
    --tui-surface: {TUI_SURFACE};
    --tui-black: {TUI_BLACK};
    --tui-border: {TUI_BORDER};
    --tui-border-active: {TUI_BORDER_ACTIVE};
    --tui-fg: {TUI_FG};
    --tui-muted: {TUI_MUTED};
    --tui-green: {TUI_GREEN};
    --tui-cyan: {TUI_CYAN};
    --tui-amber: {TUI_AMBER};
    --tui-red: {TUI_RED};
    --tui-font: {TUI_FONT_STACK};
  }}

  /* Global: monospace, square corners, no shadows */
  html, body, #q-app, .nicegui-content,
  .q-card, .q-btn, .q-field, .q-field__control, .q-field__native,
  .q-badge, .q-tab, .q-tabs, .q-tab-panel, .q-menu, .q-item,
  .q-tooltip, .q-notification, .q-dialog__inner > div, .q-header,
  input, textarea, select, code, pre, .font-mono {{
    font-family: var(--tui-font) !important;
    border-radius: 0 !important;
    box-shadow: none !important;
  }}

  body, .nicegui-content, .q-page, .q-tab-panels {{
    background-color: var(--tui-bg) !important;
    color: var(--tui-fg) !important;
  }}

  .q-ripple, .q-focus-helper {{
    display: none !important;
  }}

  /* Buttons: keycap style */
  .q-btn {{
    letter-spacing: 0.04em;
    text-transform: none !important;
    transition: background-color 0.08s ease, color 0.08s ease, border-color 0.08s ease;
  }}

  .q-btn:hover {{
    filter: brightness(1.15);
  }}

  /* Tabs: tmux-style index tabs, no sliding indicator */
  .q-tabs {{
    min-height: 0 !important;
  }}

  .q-tab {{
    min-height: 30px !important;
    color: var(--tui-muted) !important;
    border-right: 1px solid var(--tui-border);
  }}

  .q-tab__label {{
    font-size: 0.75rem !important;
    letter-spacing: 0.04em;
  }}

  .q-tab__indicator {{
    display: none !important;
  }}

  .q-tab--active {{
    background-color: var(--tui-bg) !important;
    color: var(--tui-cyan) !important;
    box-shadow: inset 0 -1px 0 var(--tui-cyan) !important;
  }}

  /* Layer tab bar (gateway | runbook): sits above the per-layer tab bar */
  .tui-layer-tabs {{
    background-color: var(--tui-black) !important;
    border-bottom: 1px solid var(--tui-border);
  }}

  .tui-layer-tabs .q-tab__label {{
    font-weight: 700;
    letter-spacing: 0.12em;
  }}

  .tui-layer-tabs .q-tab--active {{
    background-color: var(--tui-surface) !important;
    color: var(--tui-amber) !important;
    box-shadow: inset 0 -2px 0 var(--tui-amber) !important;
  }}

  /* App shell (ConsoleTemplate): the page is exactly the viewport below the q-header and never scrolls.
     Header, tab bars and the drawer keep their own height; `.tui-fill` tab panels take the rest,
     growing and shrinking with the window. Overflow scrolls inside the panes only. */
  .q-page-container.tui-shell-container {{ height: 100vh; }}
  .q-page.tui-shell {{ height: 100%; overflow: hidden; display: flex; flex-direction: column; }}
  .q-page.tui-shell > .nicegui-content {{ flex: 1 1 auto; min-height: 0; }}
  .tui-shell .q-tabs {{ flex-shrink: 0; }}
  .q-tab-panels.tui-fill,
  .q-tab-panels.tui-fill > .q-panel,
  .q-tab-panels.tui-fill > .q-panel > .q-tab-panel {{ flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }}

  /* Fields */
  .q-field--outlined .q-field__control:before {{
    border-color: var(--tui-border) !important;
  }}

  .q-field--outlined.q-field--focused .q-field__control:before,
  .q-field--outlined .q-field__control:after {{
    border-color: var(--tui-border-active) !important;
    border-width: 1px !important;
  }}

  /* Toggle / checkbox: square */
  .q-toggle__track, .q-checkbox__bg, .q-radio__bg {{
    border-radius: 0 !important;
  }}

  .q-toggle__thumb:after {{
    border-radius: 0 !important;
    box-shadow: none !important;
  }}

  .q-toggle__thumb:before {{
    display: none !important;
  }}

  /* Overlays */
  .q-menu {{
    background-color: var(--tui-surface) !important;
    border: 1px solid var(--tui-border);
  }}

  .q-tooltip {{
    background-color: var(--tui-black) !important;
    border: 1px solid var(--tui-border);
    color: var(--tui-fg) !important;
    font-size: 11px !important;
  }}

  /* Notifications: dark box + semantic border/fg instead of filled color */
  .q-notification.q-notification {{
    background-color: var(--tui-black) !important;
    border: 1px solid var(--tui-border);
    color: var(--tui-fg) !important;
    font-size: 12px;
  }}
  .q-notification.bg-positive {{ border-color: var(--tui-green); color: var(--tui-green) !important; }}
  .q-notification.bg-info     {{ border-color: var(--tui-cyan);  color: var(--tui-cyan) !important; }}
  .q-notification.bg-warning  {{ border-color: var(--tui-amber); color: var(--tui-amber) !important; }}
  .q-notification.bg-negative {{ border-color: var(--tui-red);   color: var(--tui-red) !important; }}

  /* CodeMirror (oneDark) -> terminal black */
  .cm-editor, .cm-gutters {{
    background-color: var(--tui-black) !important;
  }}

  .cm-gutters {{
    border-right: 1px solid var(--tui-border) !important;
  }}

  /* ui.code blocks */
  .nicegui-code {{
    background-color: var(--tui-black) !important;
    border: 1px solid var(--tui-border);
    border-radius: 0 !important;
  }}

  /* Linear progress: square bar on terminal-black track */
  .q-linear-progress {{
    border-radius: 0 !important;
    background-color: var(--tui-black) !important;
  }}

  /* Tables (q-table) */
  .q-table__container, .q-table__card {{
    background-color: var(--tui-black) !important;
    color: var(--tui-fg) !important;
    border-radius: 0 !important;
    box-shadow: none !important;
  }}

  .q-table--bordered {{
    border: 1px solid var(--tui-border) !important;
  }}

  .q-table th {{
    background-color: var(--tui-surface) !important;
    color: var(--tui-muted) !important;
    font-weight: 700;
  }}

  .q-table th, .q-table td {{
    border-color: var(--tui-border) !important;
  }}

  .q-table tbody tr:hover td {{
    background-color: var(--tui-surface);
  }}

  /* List rows (registries, rosters) */
  .tui-row {{
    border-bottom: 1px solid var(--tui-border);
  }}

  .tui-row:hover {{
    background-color: var(--tui-surface);
  }}

  /* Header statusline */
  .q-header.tui-header {{
    background-color: var(--tui-surface) !important;
    color: var(--tui-fg) !important;
    border-bottom: 1px solid var(--tui-border);
  }}

  .tui-title {{
    color: var(--tui-fg);
    font-size: 0.95rem;
    font-weight: 800;
    letter-spacing: 0.08em;
  }}

  .tui-statusline {{
    background-color: var(--tui-black);
    border: 1px solid var(--tui-border);
    padding: 0.15rem 0.75rem;
  }}

  /* Tags: size/wrap defaults live in a low cascade layer so Tailwind utilities
     (text-*, whitespace-*) always override them (Tailwind v3 unlayered, v4 @layer utilities). */
  .tui-tag {{
    display: inline-flex;
    align-items: center;
    padding: 0 0.4rem;
    border: 1px solid var(--tui-border);
    background-color: var(--tui-black);
    line-height: 1.6;
  }}

  @layer components {{
    .tui-tag, .tui-plain {{
      font-size: 11px;
      white-space: nowrap;
    }}
  }}

  /* Semantic border colors (e.g. ModalTemplate.border_cls) */
  .tui-border-green {{ border-color: var(--tui-green) !important; }}
  .tui-border-cyan  {{ border-color: var(--tui-cyan) !important; }}
  .tui-border-amber {{ border-color: var(--tui-amber) !important; }}
  .tui-border-red   {{ border-color: var(--tui-red) !important; }}

  .tui-sep {{
    color: var(--tui-border);
    font-size: 0.75rem;
  }}

  /* Text color utilities */
  .tui-text-fg    {{ color: var(--tui-fg) !important; }}
  .tui-text-muted {{ color: var(--tui-muted) !important; }}
  .tui-text-green {{ color: var(--tui-green) !important; }}
  .tui-text-cyan  {{ color: var(--tui-cyan) !important; }}
  .tui-text-amber {{ color: var(--tui-amber) !important; }}
  .tui-text-red   {{ color: var(--tui-red) !important; }}

  /* WebTUI Legend Box: title sits across the top 1px border.
     Parents must not clip overflow at the top edge (see kit.split_studio). */
  .tui-legend-box {{
    position: relative;
    border: 1px solid var(--tui-border);
    background-color: var(--tui-surface);
  }}

  .tui-legend-title {{
    position: absolute;
    top: -0.68em;
    left: 0.65rem;
    padding: 0 0.35rem;
    background-color: var(--tui-surface);
    color: var(--tui-cyan);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    line-height: 1.2;
    white-space: nowrap;
    z-index: 2;
  }}

  /* Right-aligned controls on the top border */
  .tui-legend-actions {{
    position: absolute;
    top: -0.85em;
    right: 0.65rem;
    padding: 0 0.25rem;
    background-color: var(--tui-surface);
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: nowrap;
    z-index: 2;
  }}

  .tui-legend-box-black {{
    background-color: var(--tui-black);
  }}

  .tui-legend-box-black .tui-legend-title,
  .tui-legend-box-black .tui-legend-actions {{
    background-color: var(--tui-black);
  }}

  /* Modal: transparent shell; its top padding keeps the legend inside q-dialog's scroll box */
  .q-card.tui-modal-shell {{
    background: transparent !important;
  }}

  .q-splitter__separator {{
    background-color: var(--tui-border) !important;
  }}

  ::-webkit-scrollbar {{
    width: 6px;
    height: 6px;
  }}

  ::-webkit-scrollbar-track {{
    background: var(--tui-black);
  }}

  ::-webkit-scrollbar-thumb {{
    background: var(--tui-border);
  }}

  ::-webkit-scrollbar-thumb:hover {{
    background: var(--tui-muted);
  }}
</style>
"""


def apply_webtui_theme() -> None:
    """Inject WebTUI stylesheet, map Quasar brand colors to TUI tokens, enable dark mode."""
    ui.dark_mode().enable()
    ui.colors(
        primary=TUI_CYAN,
        secondary=TUI_MUTED,
        accent=TUI_BORDER_ACTIVE,
        dark=TUI_SURFACE,
        positive=TUI_GREEN,
        negative=TUI_RED,
        info=TUI_CYAN,
        warning=TUI_AMBER,
    )
    ui.add_head_html(_WEB_TUI_CSS)