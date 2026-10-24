"""AprilTag 36h11 IDs 0-20 on one page: 25 mm black square, 2 mm white margin, dashed cut line, ID label."""
import cv2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

MM = 1 / 25.4  # inch
PAGE_W, PAGE_H = 215.9, 279.4  # US Letter (mm); the layout also fits A4
TAG, MARGIN, GAP, COLS = 25.0, 2.0, 8.0, 5
ids = range(21)
d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)

fig = plt.figure(figsize=(PAGE_W * MM, PAGE_H * MM))
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, PAGE_W)
ax.set_ylim(PAGE_H, 0)  # y down, in mm
ax.axis("off")
cell = TAG + 2 * MARGIN
grid_w = COLS * cell + (COLS - 1) * GAP
x0, y0 = (PAGE_W - grid_w) / 2, 30
ax.text(PAGE_W / 2, 14, "AprilTag 36h11  IDs 0-20  |  black square 25 mm, white margin 2 mm", ha="center", fontsize=10)
ax.text(PAGE_W / 2, 20, "Print at 100% / Actual size (no 'fit to page'). Check: the bar below must measure 100 mm.",
        ha="center", fontsize=7.5)
for i in ids:
    r, c = divmod(i, COLS)
    x, y = x0 + c * (cell + GAP), y0 + r * (cell + GAP)
    ax.add_patch(Rectangle((x, y), cell, cell, fill=False, ls=(0, (2, 2)), lw=0.4, ec="0.6"))  # cut line
    bits = cv2.aruco.generateImageMarker(d, i, 8, borderBits=1)  # 8 x 8 cells incl. the black border
    s = TAG / 8
    for by in range(8):
        for bx in range(8):
            if bits[by, bx] < 128:
                ax.add_patch(Rectangle((x + MARGIN + bx * s, y + MARGIN + by * s), s, s, fc="k", ec="k", lw=0.05))
    ax.text(x + cell / 2, y + cell + 3.2, f"ID {i}", ha="center", fontsize=7)
yb = y0 + 5 * (cell + GAP) + 6
ax.add_patch(Rectangle(((PAGE_W - 100) / 2, yb), 100, 2, fc="k"))
ax.text(PAGE_W / 2, yb + 6, "100 mm", ha="center", fontsize=7)
fig.savefig("outputs/apriltags/apriltag36h11_ids0-20_25mm.pdf")
fig.savefig("outputs/apriltags/preview.png", dpi=110)
