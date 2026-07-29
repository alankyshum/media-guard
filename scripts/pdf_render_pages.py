#!/usr/bin/env python3
"""Render PDF pages to WebP images using PyMuPDF + Pillow.

Usage: pdf_render_pages.py <pdf_path> <output_dir> [--max-pages N] [--long-edge PX]

Output: <output_dir>/page-<NNNN>.webp for each page up to max-pages.
"""

import argparse
import io
import os
import pathlib
import sys

from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_path")
    parser.add_argument("output_dir")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--long-edge", type=int, default=1568)
    args = parser.parse_args()

    import fitz

    os.makedirs(args.output_dir, exist_ok=True)
    doc = fitz.open(args.pdf_path)
    total = len(doc)
    rendered = min(total, args.max_pages)

    for i in range(rendered):
        page = doc[i]
        rect = page.rect
        zoom = args.long_edge / max(rect.width, rect.height)
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img_bytes = pix.tobytes("png")
        pil_img = Image.open(io.BytesIO(img_bytes))
        # Ensure RGB (WebP does not support RGBA from pixmap)
        if pil_img.mode == "RGBA":
            pil_img = pil_img.convert("RGB")
        page_path = os.path.join(args.output_dir, f"page-{i + 1:04d}.webp")
        pil_img.save(page_path, "WEBP", quality=75)

    # Write completion marker: rendered_count, total_count
    pathlib.Path(os.path.join(args.output_dir, ".complete")).write_text(
        f"{rendered}\n{total}\n"
    )
    print("OK")


if __name__ == "__main__":
    main()
