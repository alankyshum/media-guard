#!/usr/bin/env python3
"""Generate N text-bearing PNGs for media-guard OCR testing.

Each image renders "MEDIAGUARD OCR TOKEN <i:04d>" in large black Arial on white.
Output dir is created if needed.
Usage: gen_images.py <outdir> <count>
"""
import os, sys, subprocess

# If PIL is not available in current env, try the known venv we installed earlier
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    venv_py = "/tmp/pil-venv/bin/python3"
    if os.path.exists(venv_py):
        os.execv(venv_py, [venv_py] + sys.argv)
    # Last attempt: install on the fly
    subprocess.run([sys.executable, "-m", "pip", "install", "pillow", "-q"], check=True)
    from PIL import Image, ImageDraw, ImageFont

W = 800
H = 120
FONT_SIZE = 48
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Times.ttc",
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
]
font = None
for fp in FONT_CANDIDATES:
    if os.path.exists(fp):
        try:
            font = ImageFont.truetype(fp, FONT_SIZE)
            break
        except Exception:
            continue
if font is None:
    font = ImageFont.load_default()
    print(f"Warning: using default PIL font (may be illegible to tesseract).", file=sys.stderr)

def main():
    outdir = sys.argv[1]
    count = int(sys.argv[2])
    os.makedirs(outdir, exist_ok=True)

    for i in range(1, count + 1):
        img = Image.new("RGB", (W, H), "white")
        draw = ImageDraw.Draw(img)
        text = f"MEDIAGUARD OCR TOKEN {i:04d}"
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (W - tw) // 2
        y = (H - th) // 2 - 4
        draw.text((x, y), text, fill="black", font=font)
        path = os.path.join(outdir, f"img{i:04d}.png")
        img.save(path, "PNG")

    print(f"Generated {count} images in {outdir}", file=sys.stderr)

    # Verify one image OCRs correctly
    test_path = os.path.join(outdir, "img0001.png")
    if os.path.exists(test_path):
        try:
            r = subprocess.run(["tesseract", test_path, "stdout"],
                               capture_output=True, text=True, timeout=15)
            out = r.stdout.strip()
            if "MEDIAGUARD" in out:
                print(f"Verify: tesseract reads '{out[:60]}...' -> OK", file=sys.stderr)
            else:
                print(f"Verify WARNING: tesseract output '{out[:80]}' does not contain expected token", file=sys.stderr)
        except Exception as e:
            print(f"Verify error: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
