"""Render a GitHub contribution calendar as a pixel-art forest SVG.

Busy days grow trees (a different shape per commit range); empty days get a
dead stick. Trees are taller than a cell and drawn
back-to-front, so they overlap like a 3/4-view forest.

Drop a PNG in assets/sprites/<name>.png (tree1..tree4, stick) to replace any sprite with your own art.

    python scripts/forest.py --user turtle-geek
    python scripts/forest.py --demo --out-dir preview
"""
import argparse
import base64
import datetime as dt
import html
import pathlib
import random
import re
import struct
import urllib.request

PITCH_X, PITCH_Y = 16, 14  # cell spacing in SVG units; sprites are 18 wide, up to 26 tall
LEFT, TOP, BOTTOM = 34, 44, 34
SPRITE_DIR = pathlib.Path(__file__).resolve().parent.parent / "assets" / "sprites"

SHADOW = "rgba(0,0,0,0.5)"
LIGHT_SHADOW = "rgba(0,0,0,0.18)"
LIGHT_RIM = "rgba(255,255,255,0.75)"
RIM = "rgba(0,0,0,0.6)"  # soft drop shadow; only shows where it falls on another tree
TRUNK = {"t": "#7a5638", "T": "#5a3e29", "s": SHADOW, "r": RIM}
DEAD = {"x": "#54443a", "w": "#433629", "W": "#33291f", "V": "#261e17", "s": SHADOW}
LIGHT_DEAD = {"x": "#bfb0a2", "w": "#a59382", "W": "#8c7a68", "V": "#766553", "s": "rgba(0,0,0,0.12)"}

# Leaf palettes per level: a=highlight b=light c=mid d=underside
LEAVES = {
    1: {"b": "#1f5534", "c": "#19462b", "d": "#11331f"},
    2: {"b": "#2f7a3c", "c": "#256632", "d": "#1a4f27"},
    3: {"a": "#6cc24a", "b": "#4ea53e", "c": "#398a35", "d": "#276a2a"},
    4: {"a": "#c4f07a", "b": "#8fdc55", "c": "#5dbd43", "d": "#2f7a2e"},
}
CUTS = (1, 2, 5, 10)  # commits needed for tree1..tree4

# One silhouette per level, and height grows with commits (7 < 10 < 11 < 13 rows):
# small pine < pine < round tree < tall oval tree.
# Light comes from the top-left and every canopy has a dark underside, so a
# tree's bright top always reads against the dark bottom of the one behind it.
# Keep the bottom (height - 7) rows dark: that is the part the tree in front covers.
SHAPES = {
    1: ["....c....",
        "...bcd...",
        "..bccdd..",
        ".bbccddd.",
        "..ddddd..",
        "....t....",
        "..ssTss.."],
    2: ["....b....",
        "...bbc...",
        "..bbccd..",
        "...bcd...",
        "..bbccd..",
        ".bbbccdd.",
        "bbbcccddd",
        ".ddddddd.",
        "....t....",
        "..ssTss.."],
    3: ["...aab...",
        "..aabbc..",
        ".aabbbcc.",
        ".abbbccd.",
        "aabbbccdd",
        "abbbcccdd",
        ".bbbcccd.",
        "..cdddd..",
        "....t....",
        "....t....",
        "..ssTss.."],
    4: ["...aab...",
        "..aaabb..",
        "..aabbc..",
        ".aaabbcc.",
        ".aabbbcc.",
        ".ccbbbdd.",  # inner layer line: a darker band that sags in the
        "aaacccbcd",  # middle (a downward curve), inside the same silhouette
        "aabbbbccd",
        "abbbbcccd",
        ".bbbcccd.",
        "..ccddd..",
        "....t....",
        "..ssTss.."],
}
TREES = {f"tree{lvl}": (lvl, rows) for lvl, rows in SHAPES.items()}
SCALE = 2  # each art pixel is 2x2 SVG units


def with_rim(rows):
    """Pad by one pixel on the sides and top and give the canopy a standard
    pixel-art outline 'r' (a translucent shadow) on its top and sides: any empty
    pixel directly above, left or right of a leaf. No diagonals, so it steps
    with the canopy instead of wrapping round corners. Over the tree behind it
    reads as a gap; on the background it vanishes."""
    grid = [["."] * (len(rows[0]) + 2)] + [list("." + r + ".") for r in rows]
    leaf = lambda j, i: 0 <= j < len(grid) and 0 <= i < len(grid[0]) and grid[j][i] in "abcd"
    out = [row[:] for row in grid]
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            if ch == "." and (leaf(y + 1, x) or leaf(y, x - 1) or leaf(y, x + 1)):
                out[y][x] = "r"
    return ["".join(r) for r in out]


# Empty days all get the same dead stick, so they read as one quiet pattern.
BARREN = {
    "stick": [
        "..w......",
        "...w.w...",
        "....W....",
        "....W....",
        "...sWs..."],
}


def sprite_rects(rows, palette, px=1):
    """Turn ASCII art into <rect>s, merging horizontal runs of one colour."""
    out = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            ch = row[x]
            if palette.get(ch, "none") == "none":
                x += 1
                continue
            run = 1
            while x + run < len(row) and row[x + run] == ch:
                run += 1
            out.append(f'<rect x="{x * px}" y="{y * px}" width="{run * px}" '
                       f'height="{px}" fill="{palette[ch]}"/>')
            x += run
    return "".join(out)


def custom_sprite(name):
    """assets/sprites/<name>.png overrides the built-in art for that sprite.
    One PNG pixel = one SVG unit (half an art pixel). Returns (svg, width, height) or None."""
    path = SPRITE_DIR / f"{name}.png"
    if not path.exists():
        return None
    data = path.read_bytes()
    w, h = struct.unpack(">II", data[16:24])  # PNG IHDR width/height
    uri = "data:image/png;base64," + base64.b64encode(data).decode()
    return (f'<image href="{uri}" width="{w}" height="{h}" style="image-rendering:pixelated"/>', w, h)


def sprites(theme):
    """id -> (svg, width, height) in SVG units; every sprite is bottom-anchored."""
    out = {}
    for name, (lvl, rows) in TREES.items():
        rows = with_rim(rows)
        # on white a dark rim reads as a grey outline, so light mode uses a white one
        trunk = TRUNK if theme == "dark" else {**TRUNK, "r": LIGHT_RIM, "s": LIGHT_SHADOW}
        out[name] = (sprite_rects(rows, {**trunk, **LEAVES[lvl]}, SCALE),
                     len(rows[0]) * SCALE, len(rows) * SCALE)
    for name, rows in BARREN.items():
        dead = DEAD if theme == "dark" else LIGHT_DEAD
        out[name] = (sprite_rects(rows, dead, SCALE), len(rows[0]) * SCALE, len(rows) * SCALE)
    for name in out:
        out[name] = custom_sprite(name) or out[name]
    return out


def fetch(user):
    url = f"https://github.com/users/{user}/contributions"
    req = urllib.request.Request(url, headers={"User-Agent": "forest-svg"})
    page = urllib.request.urlopen(req, timeout=30).read().decode()
    counts = {}
    for cell, text in re.findall(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]+)</tool-tip>', page):
        n = re.match(r"\s*(\d+)", text)
        counts[cell] = int(n.group(1)) if n else 0
    days = []
    for td in re.findall(r"<td[^>]*data-date[^>]*>", page):
        date = re.search(r'data-date="([\d-]+)"', td).group(1)
        cell = re.search(r'id="([^"]+)"', td).group(1)
        days.append((date, counts.get(cell, 0)))
    total = re.search(r"([\d,]+)\s+contributions?\s+in the last year", page)
    return levels(sorted(days)), (total.group(1) if total else None)


def levels(days):
    """Map commit counts to tree shapes: 1 = small pine, 2-4 = tall pine,
    5-9 = round tree, 10+ = big tree. Fixed cut-offs so a shape always means the same."""
    return [(d, sum(n >= c for c in CUTS)) for d, n in days]


def demo_days():
    rng = random.Random(7)
    end = dt.date.today()
    start = end - dt.timedelta(days=364 + (end.weekday() + 1) % 7)
    days, d = [], start
    while d <= end:
        busy = 0.75 if d.month in (11, 12, 1, 2, 10) else 0.3
        level = rng.choice([1, 2, 2, 3, 3, 4]) if rng.random() < busy else 0
        days.append((d.isoformat(), level))
        d += dt.timedelta(days=1)
    return days, "717"


def render(days, total, theme="dark"):
    first = dt.date.fromisoformat(days[0][0])
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)  # Sunday
    cols = (dt.date.fromisoformat(days[-1][0]) - start).days // 7 + 1
    width = LEFT + cols * PITCH_X + 8
    grid_bottom = TOP + 7 * PITCH_Y
    height = grid_bottom + BOTTOM
    sp = sprites(theme)

    placed, months, last_month = [], [], None
    for date, level in days:
        d = dt.date.fromisoformat(date)
        offset = (d - start).days
        col, row = offset // 7, offset % 7
        ref = f"tree{level}" if level else "stick"
        _, w, h = sp[ref]
        x = LEFT + col * PITCH_X + (PITCH_X - w) // 2
        base = TOP + (row + 1) * PITCH_Y
        placed.append((row, col, ref, x, base - h))
        if row == 0 and d.month != last_month and d.day <= 7:
            months.append(f'<text x="{LEFT + col * PITCH_X}" y="14">{d.strftime("%b")}</text>')
            last_month = d.month

    # Back-to-front: higher rows first so lower trees overlap them
    placed.sort()
    body = "".join(f'<use href="#{ref}" x="{x}" y="{y}"/>' for _, _, ref, x, y in placed)

    weekdays = "".join(
        f'<text x="0" y="{TOP + (r + 1) * PITCH_Y - 3}">{name}</text>'
        for r, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")))

    legend_refs = ["stick", "tree1", "tree2", "tree3", "tree4"]
    step = 18
    lx = width - 34 - len(legend_refs) * step
    ly = grid_bottom + 26
    legend = (f'<text x="{lx - 30}" y="{ly}">Less</text>'
              + "".join(f'<use href="#{r}" x="{lx + i * step}" y="{ly + 3 - sp[r][2]}"/>'
                        for i, r in enumerate(legend_refs))
              + f'<text x="{lx + len(legend_refs) * step + 2}" y="{ly}">More</text>')
    caption = (f'<text x="{LEFT}" y="{ly}">{html.escape(total)} contributions in the last year</text>'
               if total else "")

    defs = "".join(f'<g id="{k}">{svg}</g>' for k, (svg, _, _) in sp.items())
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" shape-rendering="crispEdges">'
            '<style>text{font:11px -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;fill:#8b949e}</style>'
            f'<defs>{defs}</defs>'
            f'{"".join(months)}{weekdays}{body}{legend}{caption}</svg>')


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--user", default="turtle-geek")
    p.add_argument("--out-dir", default="assets", help="writes forest-dark.svg and forest-light.svg")
    p.add_argument("--demo", action="store_true", help="use fake data, no network")
    args = p.parse_args()
    days, total = demo_days() if args.demo else fetch(args.user)
    for theme in ("dark", "light"):
        out = pathlib.Path(args.out_dir) / f"forest-{theme}.svg"
        out.write_text(render(days, total, theme), encoding="utf-8")
        print(f"wrote {out} ({len(days)} days, {total} contributions)")


if __name__ == "__main__":
    main()
