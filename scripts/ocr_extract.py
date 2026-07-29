#!/usr/bin/env python3
"""
OCR-based PDF text extraction using PyMuPDF
"""
import sys
import fitz  # PyMuPDF
import json
import shutil
import subprocess
import os


def ocr_language():
    """Select installed Tesseract languages without hiding a missing dependency."""
    try:
        tesseract = os.environ.get("MEDIA_GUARD_TESSERACT") or shutil.which("tesseract")
        if not tesseract:
            raise RuntimeError(
                "Tesseract is required for scanned-PDF OCR. Install it with: "
                "brew install tesseract tesseract-lang, or set MEDIA_GUARD_TESSERACT"
            )
        listed = subprocess.run(
            [tesseract, "--list-langs"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Tesseract is required for scanned-PDF OCR. Install it with: "
            "brew install tesseract tesseract-lang, or set MEDIA_GUARD_TESSERACT"
        ) from exc
    installed = set(listed[1:])
    preferred = [lang for lang in ("chi_sim", "chi_tra", "eng") if lang in installed]
    if not preferred:
        raise RuntimeError(
            "Tesseract has no usable language data. Install it with: "
            "brew install tesseract tesseract-lang, or set MEDIA_GUARD_TESSERACT"
        )
    return "+".join(preferred)

def extract_with_ocr(pdf_path, page_nums=None):
    """Extract text from PDF pages using OCR fallback"""
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        
        if page_nums is None:
            page_nums = range(total_pages)
        
        results = []
        pages_with_text = 0
        pages_empty = 0
        
        for page_num in page_nums:
            if page_num >= total_pages:
                continue
                
            page = doc[page_num]
            
            # Try text extraction first
            text = page.get_text()
            
            if text.strip():
                results.append({
                    "page": page_num + 1,
                    "text": text,
                    "method": "direct"
                })
                pages_with_text += 1
            else:
                # Convert page to image and extract text from image
                try:
                    if not (os.environ.get("MEDIA_GUARD_TESSERACT") or shutil.which("tesseract")):
                        raise RuntimeError("Tesseract is required for scanned-PDF OCR but was not found. Install it with: brew install tesseract tesseract-lang, or set MEDIA_GUARD_TESSERACT")
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x zoom for better quality
                    text_page = page.get_textpage_ocr(flags=0, language=ocr_language())
                    # PyMuPDF returns a TextPage object here, not the extracted
                    # string.  Calling strip() on it breaks the scanned-PDF path.
                    img_text = text_page.extractText() if text_page else ""

                    if img_text.strip():
                        results.append({
                            "page": page_num + 1,
                            "text": img_text,
                            "method": "ocr"
                        })
                        pages_with_text += 1
                    else:
                        results.append({
                            "page": page_num + 1,
                            "text": "",
                            "method": "empty"
                        })
                        pages_empty += 1
                except FileNotFoundError as e:
                    results.append({
                        "page": page_num + 1,
                        "text": "[OCR Error: Tesseract is required but was not found. Install it with: brew install tesseract tesseract-lang, or set MEDIA_GUARD_TESSERACT]",
                        "method": "error"
                    })
                    pages_empty += 1
                except Exception as e:
                    message = str(e)
                    if "tesseract" in message.lower() or "language data" in message.lower():
                        message = f"{message}. Fix with: brew install tesseract tesseract-lang, or set MEDIA_GUARD_TESSERACT"
                    results.append({
                        "page": page_num + 1,
                        "text": f"[OCR Error: {message}]",
                        "method": "error"
                    })
                    pages_empty += 1
        
        doc.close()
        
        return {
            "total_pages_processed": len(results),
            "pages_with_text": pages_with_text,
            "pages_empty": pages_empty,
            "results": results
        }
        
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ocr_extract.py <pdf_path> [page_numbers]")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    
    # Parse page numbers if provided
    page_nums = None
    if len(sys.argv) > 2:
        page_nums = [int(p.strip()) - 1 for p in sys.argv[2].split(",")]
    
    result = extract_with_ocr(pdf_path, page_nums)
    
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)
    
    # Print results
    for item in result["results"]:
        print(f"--- Page {item['page']} [{item['method']}] ---")
        print(item["text"])
        print()
