#!/usr/bin/env python3
"""IOWAP icon: dark rounded square + cyan node-mesh mark (3 nodes + relay)."""
from PIL import Image, ImageDraw

SIZE = 512
BG = (13, 17, 23)        # deep ink
FG = (34, 211, 238)      # cyan accent
MUT = (148, 163, 184)    # slate gray lines

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded dark square
d.rounded_rectangle([16, 16, SIZE - 16, SIZE - 16], radius=96, fill=BG)

# --- mesh: three nodes + lines to a center hub -------------------------
hub = (256, 232)
n1 = (140, 344)   # bottom-left node
n2 = (372, 344)   # bottom-right node
n3 = (256, 128)   # top node

# links
for p in (n1, n2, n3):
    d.line([hub, p], fill=MUT, width=10)
# ring around hub
d.ellipse([hub[0] - 58, hub[1] - 58, hub[0] + 58, hub[1] + 58],
          outline=FG, width=14)
# hub dot
d.ellipse([hub[0] - 26, hub[1] - 26, hub[0] + 26, hub[1] + 26], fill=FG)
# leaf nodes (smaller, gray with cyan for the "home" node bottom-left)
for i, p in enumerate((n1, n2, n3)):
    r = 26
    col = FG if i == 0 else MUT
    d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=col)

# wordmark
d.text((256, 448), "IOWAP", fill=FG, anchor="mm")

img.save("brand/icon.png")

# HA add-on needs 128px icon; HACS is fine with the square png
for s in (256, 128):
    img.resize((s, s), Image.LANCZOS).save(f"brand/icon-{s}.png")
print("icons written:", [f"brand/icon-{s}.png" for s in (256, 128)], "+ brand/icon.png")