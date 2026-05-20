# -*- coding: utf-8 -*-
"""Genera icon.ico per ThicknessViewer (simbolo ISO misura spessore)."""
from PIL import Image, ImageDraw
import math, os

def draw_arrow(draw, x0, y0, x1, y1, color, lw, head):
    """Disegna una freccia da (x0,y0) a (x1,y1)."""
    draw.line([(x0, y0), (x1, y1)], fill=color, width=lw)
    angle = math.atan2(y1 - y0, x1 - x0)
    for da in (0.45, -0.45):
        ax = x1 - head * math.cos(angle + da)
        ay = y1 - head * math.sin(angle + da)
        draw.line([(x1, y1), (ax, ay)], fill=color, width=lw)

def make_icon(size):
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    s = size
    c = s // 2          # centro
    col = (40, 40, 40, 255)
    lw  = max(2, s // 32)
    head = max(6, s // 16)

    # ── due barre verticali (sezione materiale) ───────────────
    bw = max(4, s // 20)   # spessore barra
    gap = s // 5           # interasse centro-barra
    bx1 = c - gap // 2 - bw   # bordo sinistro barra sinistra
    bx2 = c - gap // 2        # bordo destro  barra sinistra
    bx3 = c + gap // 2        # bordo sinistro barra destra
    bx4 = c + gap // 2 + bw   # bordo destro  barra destra
    by0 = s // 8
    by1 = s - s // 8

    # tratteggio tra le barre
    hatch_col = (90, 90, 90, 180)
    step = max(4, s // 30)
    for i, yy in enumerate(range(by0, by1, step)):
        if i % 2 == 0:
            d.line([(bx2, yy), (bx3, yy + step)], fill=hatch_col, width=max(1, lw // 2))

    d.rectangle([bx1, by0, bx2, by1], fill=col)
    d.rectangle([bx3, by0, bx4, by1], fill=col)

    # ── sei frecce che puntano verso il centro (asterisco) ────
    # Le frecce escono dal centro C verso le barre
    margin = s // 8
    arrows = [
        # (origine x, origine y, punta x, punta y)  — punta sulle barre
        (margin,      c,         bx1,      c),              # sinistra → barra sx
        (s - margin,  c,         bx4,      c),              # destra   → barra dx
        (margin,      margin,    bx1,      by0 + s // 10),  # diag alto-sx
        (margin,      s-margin,  bx1,      by1 - s // 10),  # diag basso-sx
        (s-margin,    margin,    bx4,      by0 + s // 10),  # diag alto-dx
        (s-margin,    s-margin,  bx4,      by1 - s // 10),  # diag basso-dx
    ]
    for x0, y0, x1, y1 in arrows:
        draw_arrow(d, x0, y0, x1, y1, col, lw, head)

    return img

sizes = [16, 32, 48, 64, 128, 256]

# Disegna alla risoluzione massima, poi scala
base = make_icon(256).convert("RGBA")
frames = [base.resize((s, s), Image.LANCZOS) for s in sizes]

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
# Salva come ICO multi-size in modo compatibile con Windows
frames[-1].save(
    out,
    format="ICO",
    append_images=frames[:-1],
    sizes=[(s, s) for s in sizes],
)
print(f"Creato: {out}  ({len(frames)} frame: {sizes})")
