"""Render a recorded terminal session into an animated SVG plus a static poster.

    uv run python scripts/render_terminal_svg.py [--session docs/img/demo-session.json]
        [--out docs/img/demo-terminal.svg] [--still docs/img/demo-terminal-still.svg]

The session file is a recording of real stdout (see `recorded.sources` in it for line counts and sha256 of
the full captures). Kept lines are rendered byte-for-byte; `{"elide": true}` entries become a dim ellipsis.
The animation is pure CSS keyframes inside the SVG (no script, no external fonts), so it animates inside a
GitHub README `<img>`. Commands type character by character, output lines appear with the recorded pacing,
the view scrolls like a terminal, then the last frame holds and the loop restarts. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SESSION = REPO_ROOT / "docs" / "img" / "demo-session.json"
DEFAULT_OUT = REPO_ROOT / "docs" / "img" / "demo-terminal.svg"
DEFAULT_STILL = REPO_ROOT / "docs" / "img" / "demo-terminal-still.svg"

FONT_SIZE = 14.0
CHAR_W = FONT_SIZE * 0.6  # every mainstream monospace face is 0.6em wide; commands also pin it via textLength
LINE_H = 18.0
PAD_X = 18.0
PAD_TOP = 12.0
PAD_BOTTOM = 14.0
TITLE_H = 34.0
PROMPT = "$ "
FONT_STACK = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "DejaVu Sans Mono", monospace'

COLORS = {
    "frame": "#0d1117",
    "border": "#30363d",
    "title_bar": "#161b22",
    "title": "#8b949e",
    "fg": "#c9d1d9",
    "command": "#f0f6fc",
    "prompt": "#7ee787",
    "dim": "#6e7681",
    "pass": "#3fb950",
    "refused": "#ffa198",
    "refused_bg": "#f85149",
    "cursor": "#c9d1d9",
}


@dataclass
class Row:
    """One terminal row: a prompt+command, an output line, an elision, or the final idle prompt."""

    kind: str  # "command" | "output" | "elide" | "idle"
    text: str
    reveal_s: float
    type_start_s: float = 0.0
    type_end_s: float = 0.0
    cursor_off_s: float = 0.0


@dataclass
class Timeline:
    rows: list[Row] = field(default_factory=lambda: list[Row]())
    active_s: float = 0.0
    total_s: float = 0.0


def load_session(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def build_timeline(session: dict[str, Any]) -> Timeline:
    """Turn the recorded steps into rows with absolute reveal times (seconds from loop start)."""
    timeline = Timeline()
    t = 0.0
    for step in cast(list[dict[str, Any]], session["steps"]):
        command = str(step["command"])
        t += float(step.get("pause_before_s", 0.8))
        type_s = len(command) * float(step.get("type_ms", 35)) / 1000.0
        row = Row(kind="command", text=command, reveal_s=t, type_start_s=t + 0.35)
        row.type_end_s = row.type_start_s + type_s
        t = row.type_end_s + float(step.get("pause_after_s", 1.0))
        row.cursor_off_s = t
        timeline.rows.append(row)
        line_delay = float(step.get("line_delay_s", 0.15))
        first = True
        for item in cast(list[dict[str, Any]], step["output"]):
            if not first:
                t += float(item.get("delay_s", line_delay))
            first = False
            if item.get("elide"):
                timeline.rows.append(Row(kind="elide", text="…", reveal_s=t))
            else:
                timeline.rows.append(Row(kind="output", text=str(item["text"]), reveal_s=t))
    t += 0.6
    timeline.rows.append(Row(kind="idle", text="", reveal_s=t))
    timeline.active_s = t
    timeline.total_s = t + float(session.get("hold_s", 5.0))
    return timeline


def classify(text: str) -> str:
    """Style class for an output line. Styling only; the text itself is never changed."""
    stripped = text.strip()
    if "[PASS]" in text:
        return "pass"
    if stripped.startswith("REFUSED"):
        return "refused"
    if stripped.startswith("PASS "):
        return "pass-row"
    if stripped.startswith("✓"):
        return "pass-row"
    if " passed, 0 xfail, 0 failed" in text:
        return "pass"
    if text.startswith("benchpress demo:") or text.startswith("Benchpress receipt"):
        return "head"
    if text.endswith(":") and not text.startswith(" "):
        return "section"
    return ""


def _pct(t: float, total: float) -> str:
    value = max(0.0, min(100.0, t / total * 100.0))
    return f"{value:.3f}".rstrip("0").rstrip(".") + "%"


def _num(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _scroll_offsets(timeline: Timeline, rows_visible: int) -> list[tuple[float, float]]:
    changes: list[tuple[float, float]] = []
    for index, row in enumerate(timeline.rows):
        if index >= rows_visible:
            changes.append((row.reveal_s, (index - rows_visible + 1) * LINE_H))
    return changes


def render(session: dict[str, Any], *, animate: bool) -> str:
    timeline = build_timeline(session)
    cols = int(session.get("cols", 100))
    rows_visible = int(session.get("rows", 30))
    total = timeline.total_s
    width = PAD_X * 2 + cols * CHAR_W
    body_h = PAD_TOP + rows_visible * LINE_H + PAD_BOTTOM
    height = TITLE_H + body_h
    body_top = TITLE_H + PAD_TOP
    title = str(session.get("title", "terminal"))

    css: list[str] = [
        f"text{{font-family:{FONT_STACK};font-size:{_num(FONT_SIZE)}px;white-space:pre;fill:{COLORS['fg']}}}",
        f".t{{font-size:12px;fill:{COLORS['title']};text-anchor:middle}}",
        f".p{{fill:{COLORS['prompt']};font-weight:700}}",
        f".c{{fill:{COLORS['command']};font-weight:600}}",
        f".dim{{fill:{COLORS['dim']}}}",
        f".pass{{fill:{COLORS['pass']};font-weight:700}}",
        f".pass-row{{fill:{COLORS['pass']}}}",
        f".refused{{fill:{COLORS['refused']};font-weight:700}}",
        f".head{{fill:{COLORS['command']};font-weight:700}}",
        f".section{{fill:{COLORS['title']};font-weight:700}}",
    ]
    if animate:
        css.append(".a{animation-duration:" + _num(total) + "s;animation-iteration-count:infinite;"
                   "animation-timing-function:step-end;animation-fill-mode:both}")
    body: list[str] = []
    final_offset = 0.0

    def reveal(name: str, at: float) -> str:
        css.append(f"@keyframes {name}{{0%{{opacity:0}}{_pct(at, total)}{{opacity:1}}100%{{opacity:1}}}}")
        return f' class="a" style="animation-name:{name}"'

    for index, row in enumerate(timeline.rows):
        y = body_top + index * LINE_H
        baseline = y + LINE_H - 5
        attrs = reveal(f"r{index}", row.reveal_s) if animate else ""
        parts: list[str] = []
        if row.kind in ("command", "idle"):
            parts.append(f'<text x="{_num(PAD_X)}" y="{_num(baseline)}" class="p">{escape(PROMPT.strip())}</text>')
            cmd_x = PAD_X + len(PROMPT) * CHAR_W
            if row.kind == "command":
                typed_w = len(row.text) * CHAR_W
                parts.append(
                    f'<text x="{_num(cmd_x)}" y="{_num(baseline)}" class="c" textLength="{_num(typed_w)}" '
                    f'lengthAdjust="spacingAndGlyphs">{escape(row.text)}</text>'
                )
                if animate:
                    n = len(row.text)
                    start, end = _pct(row.type_start_s, total), _pct(row.type_end_s, total)
                    shift = f"translateX({_num(typed_w)}px)"
                    css.append(
                        f"@keyframes k{index}{{0%{{transform:translateX(0)}}"
                        f"{start}{{transform:translateX(0);animation-timing-function:steps({n},end)}}"
                        f"{end}{{transform:{shift}}}100%{{transform:{shift}}}}}"
                    )
                    css.append(
                        f"@keyframes u{index}{{0%{{opacity:0;transform:translateX(0)}}"
                        f"{_pct(row.reveal_s, total)}{{opacity:1;transform:translateX(0)}}"
                        f"{start}{{opacity:1;transform:translateX(0);animation-timing-function:steps({n},end)}}"
                        f"{end}{{opacity:1;transform:{shift}}}"
                        f"{_pct(row.cursor_off_s, total)}{{opacity:0;transform:{shift}}}"
                        f"100%{{opacity:0;transform:{shift}}}}}"
                    )
                    parts.append(
                        f'<rect x="{_num(cmd_x - 1)}" y="{_num(y)}" width="{_num(typed_w + CHAR_W + 2)}" '
                        f'height="{_num(LINE_H)}" fill="{COLORS["frame"]}" class="a" style="animation-name:k{index}"/>'
                    )
                    parts.append(
                        f'<rect x="{_num(cmd_x)}" y="{_num(y + 2)}" width="{_num(CHAR_W)}" height="{_num(LINE_H - 3)}" '
                        f'fill="{COLORS["cursor"]}" class="a" style="animation-name:u{index}"/>'
                    )
            else:
                cursor_attrs = ""
                if animate:
                    stops = [f"0%{{opacity:0}}{_pct(row.reveal_s, total)}{{opacity:1}}"]
                    t, on = row.reveal_s + 0.55, False
                    while t < total - 0.05:
                        stops.append(f"{_pct(t, total)}{{opacity:{1 if on else 0}}}")
                        t, on = t + 0.55, not on
                    stops.append("100%{opacity:0}")
                    css.append(f"@keyframes b{index}{{{''.join(stops)}}}")
                    cursor_attrs = f' class="a" style="animation-name:b{index}"'
                parts.append(
                    f'<rect x="{_num(cmd_x)}" y="{_num(y + 2)}" width="{_num(CHAR_W)}" height="{_num(LINE_H - 3)}" '
                    f'fill="{COLORS["cursor"]}"{cursor_attrs}/>'
                )
        elif row.kind == "elide":
            parts.append(f'<text x="{_num(PAD_X)}" y="{_num(baseline)}" class="dim">{escape(row.text)}</text>')
        else:
            style = classify(row.text)
            if style == "refused":
                indent = len(row.text) - len(row.text.lstrip())
                parts.append(
                    f'<rect x="{_num(PAD_X + indent * CHAR_W - 4)}" y="{_num(y + 1)}" '
                    f'width="{_num(len(row.text.strip()) * CHAR_W + 8)}" height="{_num(LINE_H - 1)}" rx="3" '
                    f'fill="{COLORS["refused_bg"]}" fill-opacity="0.18" stroke="{COLORS["refused_bg"]}" '
                    'stroke-opacity="0.6"/>'
                )
            class_attr = f' class="{style}"' if style else ""
            if row.text:
                parts.append(f'<text x="{_num(PAD_X)}" y="{_num(baseline)}"{class_attr}>{escape(row.text)}</text>')
        if not parts:
            continue
        body.append(f"<g{attrs}>{''.join(parts)}</g>")

    offsets = _scroll_offsets(timeline, rows_visible)
    if offsets:
        final_offset = offsets[-1][1]
    if animate and offsets:
        stops = ["0%{transform:translateY(0)}"]
        stops += [f"{_pct(at, total)}{{transform:translateY(-{_num(off)}px)}}" for at, off in offsets]
        stops.append(f"100%{{transform:translateY(-{_num(final_offset)}px)}}")
        css.append(f"@keyframes scroll{{{''.join(stops)}}}")
        scroll_attrs = ' class="a" style="animation-name:scroll"'
    else:
        scroll_attrs = f' transform="translate(0 {_num(-final_offset)})"' if final_offset else ""

    dots = "".join(
        f'<circle cx="{_num(20 + i * 20)}" cy="{_num(TITLE_H / 2)}" r="6" fill="{color}"/>'
        for i, color in enumerate(("#ff5f57", "#febc2e", "#28c840"))
    )
    label = escape(title)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_num(width)}" height="{_num(height)}" '
        f'viewBox="0 0 {_num(width)} {_num(height)}" role="img" aria-label="{label}">'
        f"<title>{label}</title>"
        f"<style>{''.join(css)}</style>"
        f'<defs><clipPath id="body"><rect x="0" y="{_num(TITLE_H)}" width="{_num(width)}" '
        f'height="{_num(body_h)}"/></clipPath></defs>'
        f'<rect x="0.5" y="0.5" width="{_num(width - 1)}" height="{_num(height - 1)}" rx="10" '
        f'fill="{COLORS["frame"]}" stroke="{COLORS["border"]}"/>'
        f'<path d="M0.5 {_num(TITLE_H)} V10.5 A10 10 0 0 1 10.5 0.5 H{_num(width - 10.5)} '
        f'A10 10 0 0 1 {_num(width - 0.5)} 10.5 V{_num(TITLE_H)} Z" fill="{COLORS["title_bar"]}"/>'
        f'<line x1="0.5" y1="{_num(TITLE_H)}" x2="{_num(width - 0.5)}" y2="{_num(TITLE_H)}" '
        f'stroke="{COLORS["border"]}"/>'
        f'{dots}<text x="{_num(width / 2)}" y="{_num(TITLE_H / 2 + 4)}" class="t">{label}</text>'
        f'<g clip-path="url(#body)"><g{scroll_attrs}>{"".join(body)}</g></g>'
        "</svg>\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--still", type=Path, default=DEFAULT_STILL)
    args = parser.parse_args(argv)
    session_path = cast(Path, args.session)
    out_path = cast(Path, args.out)
    still_path = cast(Path, args.still)
    session = load_session(session_path)
    animated = render(session, animate=True)
    still = render(session, animate=False)
    out_path.write_text(animated, encoding="utf-8")
    still_path.write_text(still, encoding="utf-8")
    timeline = build_timeline(session)
    print(
        f"{out_path} {len(animated.encode('utf-8'))} bytes, active {timeline.active_s:.1f}s, "
        f"loop {timeline.total_s:.1f}s; {still_path} {len(still.encode('utf-8'))} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
