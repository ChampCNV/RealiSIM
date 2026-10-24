"""Cut the object out of a photo (transparent background)."""
import cv2
import numpy as np
import rembg
from PIL import Image

_session = None


def cut_out(image_path, keep_box=None):
    """Returns an RGBA image with the background fully transparent and the object fully opaque.

    keep_box: (x0, y0, x1, y1) where the target object is (e.g. projected from a 3D scan); shapes whose
    centre lies outside it are dropped, for crops where a neighbouring object peeks in.
    """
    global _session
    if _session is None:
        # BiRefNet keeps low-contrast parts (a white Lego base on light wood) that isnet dropped;
        # isnet in turn was much better than rembg's default u2net, which kept half the table.
        _session = rembg.new_session("birefnet-general-lite")
    rgba = np.asarray(rembg.remove(Image.open(image_path), session=_session)).copy()
    # Drop the faint haze isnet leaves on the background, or later crops see the whole photo as object.
    # Solid mask: soft alpha on small/blurry crops left the whole object semi-transparent, and image-to-3D
    # models then treat the background square as part of the object.
    rgba[:, :, 3] = np.where(rgba[:, :, 3] >= 128, 255, 0)
    if keep_box is not None:
        x0, y0, x1, y1 = keep_box
        n, labels, stats, centroids = cv2.connectedComponentsWithStats((rgba[:, :, 3] > 0).astype(np.uint8))
        for i in range(1, n):
            cx, cy = centroids[i]
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                rgba[labels == i, 3] = 0
    return Image.fromarray(rgba)
