"""Record the README demo: drive ccs in tmux on made-up sessions and save the screens as an animated SVG.

    python demo/record.py            # writes demo/ccs.svg

Needs tmux and fzf. The sessions come from make_demo.py, so nothing of yours ends up in the picture.
Each step sends keys to ccs, waits, and captures the screen with its colors (tmux capture-pane -e);
the SVG then shows the frames one after another with a CSS animation, which GitHub plays in a README.
"""

from __future__ import annotations

import html
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
COLS, ROWS = 180, 34
CELL_W, CELL_H, FONT = 8.4, 18, 14
PAD, BAR = 16, 30
BG, FG = "#1e1f29", "#d6d6d6"

# (keys to send, seconds to show the frame); "" keys = just capture
STEPS = [("", 3.0), ("Down", 1.4), ("Down", 1.4), ("Down", 2.2),
         ("p", 0.4), ("a", 0.4), ("y", 0.4), ("m", 0.4), ("e", 0.4), ("n", 0.4), ("t", 0.4), ("s", 2.5),
         ("C-u", 0.8), ("i", 0.4), ("n", 0.4), ("f", 0.4), ("r", 0.4), ("a", 2.5), ("C-u", 1.5)]

ANSI16 = ["#1e1f29", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d6d6d6",
          "#5c6370", "#ff7a85", "#b5e890", "#ffd88a", "#7cc3ff", "#e39cf5", "#6fd6e3", "#ffffff"]
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "ccs.svg"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([sys.executable, str(HERE / "make_demo.py"), tmp, "--live-seconds", "120"], check=True)
        frames = capture(Path(tmp))
    out.write_text(svg(frames))
    print(f"{len(frames)} frames -> {out} ({out.stat().st_size // 1024} KB)")


def capture(data: Path) -> list:
    env = {"CLAUDE_CONFIG_DIR": f"{data}/claude", "XDG_CACHE_HOME": f"{data}/cache", "CCS_OFFLINE": "1",
           "PYTHONPATH": str(HERE.parent / "src")}
    command = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + f" {shlex.quote(sys.executable)} -m ccs"
    tmux = ["tmux", "-L", "ccs-demo", "-f", "/dev/null"]
    subprocess.run(tmux + ["new-session", "-d", "-s", "demo", "-x", str(COLS), "-y", str(ROWS),
                           "-e", "TERM=xterm-256color", command], check=True)
    try:
        time.sleep(2)  # first run reads the sessions and starts fzf
        frames = []
        for keys, seconds in STEPS:
            if keys:
                subprocess.run(tmux + ["send-keys", "-t", "demo", keys], check=True)
            time.sleep(0.8)  # let fzf filter and the preview load
            screen = subprocess.run(tmux + ["capture-pane", "-e", "-p", "-t", "demo"],
                                    check=True, capture_output=True, text=True).stdout
            frames.append((parse(screen), seconds))
        return frames
    finally:
        subprocess.run(tmux + ["kill-server"], check=False)


# --- ANSI screen -> styled cells ---

def parse(screen: str) -> list:
    """Rows of runs: (column, text, fg, bg, bold, dim), wide characters in runs of their own."""
    rows = []
    style = reset()
    for line in screen.split("\n")[:ROWS]:
        runs, col, pos = [], 0, 0
        for m in list(SGR.finditer(line)) + [None]:
            text = line[pos: m.start() if m else len(line)]
            for ch in text:
                width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
                key = (style["fg"], style["bg"], style["bold"], style["dim"])
                if runs and width == 1 and runs[-1][5] == key and runs[-1][4] == 1 and runs[-1][0] + len(runs[-1][1]) == col:
                    runs[-1][1] += ch
                else:
                    runs.append([col, ch, *key[:2], width, key])
                col += width
            if m:
                apply(style, m.group(1))
                pos = m.end()
        rows.append([(c, t, fg, bg, k[2], k[3], w) for c, t, fg, bg, w, k in runs])
    return rows


def reset() -> dict:
    return {"fg": None, "bg": None, "bold": False, "dim": False, "reverse": False}


def apply(style: dict, params: str) -> None:
    codes = [int(p) if p else 0 for p in params.split(";")]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            style.update(reset())
        elif c == 1:
            style["bold"] = True
        elif c == 2:
            style["dim"] = True
        elif c == 22:
            style["bold"] = style["dim"] = False
        elif c in (7, 27):
            if style["reverse"] != (c == 7):
                style["reverse"] = c == 7
                style["fg"], style["bg"] = style["bg"] or BG, style["fg"] or FG
        elif 30 <= c <= 37 or 90 <= c <= 97:
            style["fg"] = ANSI16[c - 30 if c < 90 else c - 82]
        elif 40 <= c <= 47 or 100 <= c <= 107:
            style["bg"] = ANSI16[c - 40 if c < 100 else c - 92]
        elif c == 39:
            style["fg"] = None
        elif c == 49:
            style["bg"] = None
        elif c in (38, 48) and i + 1 < len(codes):
            if codes[i + 1] == 5 and i + 2 < len(codes):
                color, i = xterm256(codes[i + 2]), i + 2
            elif codes[i + 1] == 2 and i + 4 < len(codes):
                color, i = "#%02x%02x%02x" % tuple(codes[i + 2: i + 5]), i + 4
            else:
                color = None
            style["fg" if c == 38 else "bg"] = color
        i += 1


def xterm256(n: int) -> str:
    if n < 16:
        return ANSI16[n]
    if n < 232:
        levels = [0, 95, 135, 175, 215, 255]
        n -= 16
        return "#%02x%02x%02x" % (levels[n // 36], levels[n // 6 % 6], levels[n % 6])
    gray = 8 + 10 * (n - 232)
    return "#%02x%02x%02x" % (gray, gray, gray)


# --- cells -> animated SVG ---

def svg(frames: list) -> str:
    width, height = PAD * 2 + COLS * CELL_W, BAR + PAD * 2 + ROWS * CELL_H
    total = sum(seconds for _, seconds in frames)
    css, groups, start = [], [], 0.0
    for i, (rows, seconds) in enumerate(frames):
        a, b = 100 * start / total, 100 * (start + seconds) / total
        css.append(f"#f{i}{{animation-name:k{i}}}@keyframes k{i}{{0%{{visibility:hidden}}"
                   f"{a:.3f}%{{visibility:visible}}{b:.3f}%{{visibility:hidden}}}}")
        groups.append(f'<g class="f" id="f{i}">{draw(rows)}</g>')
        start += seconds
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" font-family="Menlo, Monaco, \'DejaVu Sans Mono\', Consolas, monospace" '
        f'font-size="{FONT}">'
        # frame 0 shows where animations do not run (previews, some viewers)
        f"<style>.f{{visibility:hidden;animation:{total:.1f}s step-end infinite}}#f0{{visibility:visible}}"
        f"{''.join(css)}</style>"
        f'<rect width="100%" height="100%" rx="10" fill="{BG}"/>'
        + "".join(f'<circle cx="{PAD + 6 + 20 * k}" cy="{BAR // 2 + 2}" r="6" fill="{c}"/>'
                  for k, c in enumerate(["#ff5f57", "#febc2e", "#28c840"]))
        + "".join(groups) + "</svg>\n"
    )


def draw(rows: list) -> str:
    parts = []
    for r, runs in enumerate(rows):
        y = BAR + PAD + r * CELL_H
        for col, text, fg, bg, bold, dim, width in runs:
            x, w = PAD + col * CELL_W, len(text) * width * CELL_W
            if bg and bg != BG:
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{CELL_H}" fill="{bg}"/>')
            if not text.strip():
                continue
            attrs = f'x="{x:.1f}" y="{y + CELL_H * 0.75:.1f}" fill="{fg or FG}"'
            if width == 1:
                attrs += f' textLength="{w:.1f}" lengthAdjust="spacingAndGlyphs"'
            if bold:
                attrs += ' font-weight="bold"'
            if dim:
                attrs += ' opacity="0.6"'
            parts.append(f'<text xml:space="preserve" {attrs}>{html.escape(text, quote=False)}</text>')
    return "".join(parts)


if __name__ == "__main__":
    main()
