#!/usr/bin/env python3
"""PDF Tool - CLI for reading and manipulating PDF files."""

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def error_exit(message: str, error_type: str, code: int = 1):
    """Print a JSON error to stderr and exit."""
    payload = {"error": message, "type": error_type}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    sys.exit(code)


def validate_pdf_path(path_str: str) -> Path:
    """Validate that *path_str* points to an existing, readable PDF file."""
    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        error_exit(f"File not found: {path_str}", "FileNotFoundError", 1)
    if not p.is_file():
        error_exit(f"Not a file: {path_str}", "FileNotFoundError", 1)
    if not os.access(p, os.R_OK):
        error_exit(f"Permission denied: {path_str}", "PermissionError", 1)
    # Check PDF magic bytes
    try:
        with open(p, "rb") as fh:
            header = fh.read(5)
        if header != b"%PDF-":
            error_exit(f"Not a valid PDF file: {path_str}", "InvalidPDFError", 1)
    except OSError as exc:
        error_exit(str(exc), "IOError", 1)
    return p


def get_reader(pdf_path: Path):
    """Return a pypdf.PdfReader, handling encrypted / corrupt files."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(pdf_path))
    except PdfReadError as exc:
        error_exit(f"Failed to parse PDF: {exc}", "InvalidPDFError", 1)
    except Exception as exc:
        error_exit(str(exc), "InvalidPDFError", 1)

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            error_exit(
                "PDF is encrypted/password-protected and cannot be opened without a password",
                "EncryptedPDFError",
                1,
            )
    return reader


def _json_out(obj):
    """Print a Python object as indented JSON to stdout."""
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _extract_page_text_pdfplumber(pdf_path: Path, page_idx: int) -> str | None:
    """Try extracting text for *page_idx* (0-based) via pdfplumber.

    Returns the text string on success, or None on failure / empty result.
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            if page_idx >= len(pdf.pages):
                return None
            text = pdf.pages[page_idx].extract_text()
            if text:
                return text
    except Exception:
        pass
    return None


def _extract_page_text_pypdf(reader, page_idx: int) -> str:
    """Fallback text extraction via pypdf. Always returns a string (may be empty)."""
    try:
        return reader.pages[page_idx].extract_text() or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_read_text(args):
    pdf_path = validate_pdf_path(args.pdf_path)
    reader = get_reader(pdf_path)
    total_pages = len(reader.pages)

    # Determine which pages to extract
    if args.pages:
        try:
            page_nums = [int(p.strip()) for p in args.pages.split(",")]
        except ValueError:
            error_exit("--pages must be comma-separated integers (1-indexed)", "InvalidArgumentError", 2)
        for pn in page_nums:
            if pn < 1 or pn > total_pages:
                error_exit(
                    f"Page {pn} out of range (PDF has {total_pages} pages)",
                    "InvalidArgumentError",
                    2,
                )
    else:
        page_nums = list(range(1, total_pages + 1))

    pages_data: list[dict] = []
    for pn in page_nums:
        idx = pn - 1
        text = _extract_page_text_pdfplumber(pdf_path, idx)
        if text is None:
            text = _extract_page_text_pypdf(reader, idx)
        pages_data.append({"page": pn, "text": text})

    if args.format == "json":
        _json_out({
            "file": str(pdf_path),
            "total_pages": total_pages,
            "pages": pages_data,
        })
    else:
        parts: list[str] = []
        for pd in pages_data:
            parts.append(f"--- Page {pd['page']} ---")
            parts.append(pd["text"])
            parts.append("")
        print("\n".join(parts))


def cmd_metadata(args):
    pdf_path = validate_pdf_path(args.pdf_path)
    reader = get_reader(pdf_path)
    meta = reader.metadata

    def _get(key):
        if meta is None:
            return None
        val = meta.get(key)
        if val is None:
            return None
        return str(val)

    def _get_date(key):
        if meta is None:
            return None
        raw = meta.get(key)
        if raw is None:
            return None
        # pypdf may return a string or datetime-ish object
        if hasattr(raw, "isoformat"):
            return raw.isoformat()
        return str(raw)

    _json_out({
        "file": str(pdf_path),
        "title": _get("/Title"),
        "author": _get("/Author"),
        "subject": _get("/Subject"),
        "creator": _get("/Creator"),
        "producer": _get("/Producer"),
        "creation_date": _get_date("/CreationDate"),
        "modification_date": _get_date("/ModDate"),
        "page_count": len(reader.pages),
        "pdf_version": reader.pdf_header if hasattr(reader, "pdf_header") else None,
        "encrypted": reader.is_encrypted,
    })


def cmd_list_fields(args):
    pdf_path = validate_pdf_path(args.pdf_path)
    reader = get_reader(pdf_path)
    fields: list[dict] = []

    # Map PDF field type flags to human-readable names
    FIELD_TYPE_MAP = {
        "/Tx": "text",
        "/Sig": "signature",
    }

    def _resolve_field_type(field_obj) -> str:
        ft = field_obj.get("/FT")
        if ft is None:
            return "unknown"
        ft = str(ft)
        if ft in ("/Tx", "/Sig"):
            return FIELD_TYPE_MAP[ft]
        if ft == "/Ch":
            flags = field_obj.get("/Ff")
            flags = int(flags) if flags is not None else 0
            # Bit 18 (0x20000) = combo box
            if flags & (1 << 17):
                return "dropdown"
            return "listbox"
        if ft == "/Btn":
            flags = field_obj.get("/Ff")
            flags = int(flags) if flags is not None else 0
            # Bit 16 (0x10000) = radio button
            if flags & (1 << 15):
                return "radio"
            return "checkbox"
        return "unknown"

    def _get_options(field_obj) -> list[str] | None:
        opt = field_obj.get("/Opt")
        if opt is None:
            return None
        result = []
        for item in opt:
            if isinstance(item, (list, tuple)):
                result.append(str(item[-1]))
            else:
                result.append(str(item))
        return result

    def _find_page_for_field(field_obj, reader) -> int | None:
        """Try to determine the 1-based page number for a field."""
        # Some fields have a /P entry pointing to the page object
        page_ref = field_obj.get("/P")
        if page_ref is not None:
            for i, page in enumerate(reader.pages):
                if page.indirect_reference == page_ref:
                    return i + 1
        # Fallback: look through page annotations
        field_ref = field_obj.indirect_reference
        if field_ref is not None:
            for i, page in enumerate(reader.pages):
                annots = page.get("/Annots")
                if annots is None:
                    continue
                for annot in annots:
                    try:
                        if annot.get_object().indirect_reference == field_ref:
                            return i + 1
                    except Exception:
                        pass
        return None

    if reader.get_fields():
        for name, field_obj in reader.get_fields().items():
            ftype = _resolve_field_type(field_obj)
            value = field_obj.get("/V")
            default = field_obj.get("/DV")
            ff = field_obj.get("/Ff")
            ff = int(ff) if ff is not None else 0

            # Required: bit 2 (0x4)
            required = bool(ff & (1 << 1))
            # Read-only: bit 1 (0x2)
            read_only = bool(ff & 1)

            options = _get_options(field_obj)
            # For checkboxes / radio, extract the possible appearance states as options
            if ftype in ("checkbox", "radio") and options is None:
                ap_dict = field_obj.get("/AP")
                if ap_dict:
                    n_dict = ap_dict.get("/N")
                    if n_dict and hasattr(n_dict, "keys"):
                        options = [str(k) for k in n_dict.keys() if str(k) != "/Off"]

            page_num = _find_page_for_field(field_obj, reader)

            fields.append({
                "name": name,
                "type": ftype,
                "value": str(value) if value is not None else "",
                "default_value": str(default) if default is not None else "",
                "options": options,
                "required": required,
                "read_only": read_only,
                "page": page_num,
            })

    _json_out({
        "file": str(pdf_path),
        "field_count": len(fields),
        "fields": fields,
    })


def cmd_fill_form(args):
    pdf_path = validate_pdf_path(args.pdf_path)
    output_path = Path(args.output_path).expanduser().resolve()

    # Prevent overwriting source
    if pdf_path == output_path:
        error_exit("Output path must be different from source PDF", "InvalidArgumentError", 2)

    # Parse field data
    if args.data and args.data_file:
        error_exit("Provide either --data or --data-file, not both", "InvalidArgumentError", 2)

    if args.data:
        try:
            field_data: dict = json.loads(args.data)
        except json.JSONDecodeError as exc:
            error_exit(f"Invalid JSON in --data: {exc}", "InvalidArgumentError", 2)
    elif args.data_file:
        data_file = Path(args.data_file).expanduser().resolve()
        if not data_file.exists():
            error_exit(f"Data file not found: {args.data_file}", "FileNotFoundError", 1)
        try:
            field_data = json.loads(data_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            error_exit(f"Invalid JSON in data file: {exc}", "InvalidArgumentError", 2)
    else:
        error_exit("One of --data or --data-file is required", "InvalidArgumentError", 2)

    if not isinstance(field_data, dict):
        error_exit("Field data must be a JSON object (key/value pairs)", "InvalidArgumentError", 2)

    reader = get_reader(pdf_path)

    # Get existing field names
    existing_fields: set[str] = set()
    raw_fields = reader.get_fields()
    if raw_fields:
        existing_fields = set(raw_fields.keys())

    if not existing_fields:
        error_exit("PDF has no fillable form fields", "NoFormFieldsError", 1)

    fields_filled: list[str] = []
    fields_not_found: list[str] = []

    for name in field_data:
        if name in existing_fields:
            fields_filled.append(name)
        else:
            fields_not_found.append(name)

    # Build the filled PDF
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.append(reader)

    fill_dict = {k: v for k, v in field_data.items() if k in existing_fields}

    for page_idx in range(len(writer.pages)):
        writer.update_page_form_field_values(writer.pages[page_idx], fill_dict)

    # Flatten if requested
    if args.flatten:
        for page in writer.pages:
            annots = page.get("/Annots")
            if annots:
                # Remove annotation entries to flatten
                for i in range(len(annots) - 1, -1, -1):
                    try:
                        annot = annots[i].get_object()
                        if annot.get("/Subtype") == "/Widget":
                            del annots[i]
                    except Exception:
                        pass

    # Write output
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as fh:
            writer.write(fh)
    except PermissionError:
        error_exit(f"Permission denied writing to: {output_path}", "PermissionError", 1)
    except OSError as exc:
        error_exit(str(exc), "IOError", 1)

    _json_out({
        "status": "success",
        "output_file": str(output_path),
        "fields_filled": len(fields_filled),
        "fields_skipped": [],
        "fields_not_found": fields_not_found,
    })


# ---------------------------------------------------------------------------
# Flat-PDF filling (no AcroForm fields) — coordinate-based overlay
# ---------------------------------------------------------------------------

def cmd_scan_layout(args):
    """Extract form layout (rects, lines, labels) from a flat PDF for mapping fields."""
    pdf_path = validate_pdf_path(args.pdf_path)
    import pdfplumber

    page_height = 792.0  # will be overridden
    results = {"file": str(pdf_path), "pages": []}

    with pdfplumber.open(str(pdf_path)) as pdf:
        pages_to_scan = []
        if args.pages:
            try:
                pages_to_scan = [int(p.strip()) - 1 for p in args.pages.split(",")]
            except ValueError:
                error_exit("--pages must be comma-separated integers (1-indexed)", "InvalidArgumentError", 2)
        else:
            pages_to_scan = list(range(len(pdf.pages)))

        for idx in pages_to_scan:
            if idx < 0 or idx >= len(pdf.pages):
                error_exit(f"Page {idx+1} out of range", "InvalidArgumentError", 2)
            page = pdf.pages[idx]
            page_height = float(page.height)
            page_width = float(page.width)

            # Rectangles (form boxes)
            rects = []
            for r in sorted(page.rects, key=lambda r: (r['top'], r['x0'])):
                w = r['x1'] - r['x0']
                h = r['bottom'] - r['top']
                if w > 15 and h > 6:
                    rects.append({
                        "x0": round(r['x0'], 1),
                        "top": round(r['top'], 1),
                        "x1": round(r['x1'], 1),
                        "bottom": round(r['bottom'], 1),
                        "width": round(w, 1),
                        "height": round(h, 1),
                        "pdf_coords": {
                            "x0": round(r['x0'], 1),
                            "y_bottom": round(page_height - r['bottom'], 1),
                            "x1": round(r['x1'], 1),
                            "y_top": round(page_height - r['top'], 1),
                        },
                    })

            # Lines (underlines, box edges)
            lines = []
            for l in sorted(page.lines, key=lambda l: (l['top'], l['x0'])):
                length = ((l['x1'] - l['x0'])**2 + (l['bottom'] - l['top'])**2)**0.5
                if length > 15:
                    is_horiz = abs(l['top'] - l['bottom']) < 1
                    lines.append({
                        "x0": round(l['x0'], 1),
                        "top": round(l['top'], 1),
                        "x1": round(l['x1'], 1),
                        "bottom": round(l['bottom'], 1),
                        "orientation": "horizontal" if is_horiz else "vertical",
                    })

            # Text labels
            labels = []
            for w in sorted(page.extract_words(
                keep_blank_chars=True, extra_attrs=["fontname", "size"]
            ), key=lambda w: (w['top'], w['x0'])):
                labels.append({
                    "text": w['text'],
                    "x0": round(w['x0'], 1),
                    "top": round(w['top'], 1),
                    "x1": round(w['x1'], 1),
                    "bottom": round(w['bottom'], 1),
                    "font": w.get('fontname', ''),
                    "size": round(w.get('size', 0), 1),
                })

            results["pages"].append({
                "page": idx + 1,
                "width": page_width,
                "height": page_height,
                "rectangles": rects,
                "lines": lines,
                "labels": labels,
            })

    _json_out(results)


def cmd_overlay_fill(args):
    """Fill a flat PDF (no form fields) by overlaying text at exact coordinates."""
    pdf_path = validate_pdf_path(args.pdf_path)
    output_path = Path(args.output_path).expanduser().resolve()

    if pdf_path == output_path:
        error_exit("Output path must be different from source PDF", "InvalidArgumentError", 2)

    # Parse field data
    if args.data and args.data_file:
        error_exit("Provide either --data or --data-file, not both", "InvalidArgumentError", 2)

    if args.data:
        try:
            field_data: dict = json.loads(args.data)
        except json.JSONDecodeError as exc:
            error_exit(f"Invalid JSON in --data: {exc}", "InvalidArgumentError", 2)
    elif args.data_file:
        data_file = Path(args.data_file).expanduser().resolve()
        if not data_file.exists():
            error_exit(f"Data file not found: {args.data_file}", "FileNotFoundError", 1)
        try:
            field_data = json.loads(data_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            error_exit(f"Invalid JSON in data file: {exc}", "InvalidArgumentError", 2)
    else:
        error_exit("One of --data or --data-file is required", "InvalidArgumentError", 2)

    if not isinstance(field_data, dict):
        error_exit("Field data must be a JSON object", "InvalidArgumentError", 2)

    # Validate field entries
    for key, entry in field_data.items():
        if not isinstance(entry, dict):
            error_exit(
                f"Field '{key}' must be an object with 'text' and 'box' keys",
                "InvalidArgumentError", 2,
            )
        if "text" not in entry or "box" not in entry:
            error_exit(
                f"Field '{key}' must have 'text' and 'box' keys",
                "InvalidArgumentError", 2,
            )
        box = entry["box"]
        if not isinstance(box, list) or len(box) != 4:
            error_exit(
                f"Field '{key}' box must be [x0, y_bottom, x1, y_top] in PDF coords",
                "InvalidArgumentError", 2,
            )

    font_name = args.font or "Helvetica"
    font_size = args.font_size or 10
    padding = args.padding if args.padding is not None else 4

    from io import BytesIO
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import letter
    from pypdf import PdfReader, PdfWriter

    reader = get_reader(pdf_path)
    page = reader.pages[args.page - 1] if args.page else reader.pages[0]
    media = page.mediabox
    page_w = float(media.width)
    page_h = float(media.height)

    # Create overlay
    buf = BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))

    line_spacing = font_size * 1.3
    fields_placed = []

    for key, entry in field_data.items():
        text = str(entry["text"])
        box = entry["box"]  # [x0, y_bottom, x1, y_top] in PDF coords
        x0, y_bot, x1, y_top = box
        box_w = x1 - x0 - 2 * padding
        box_h = y_top - y_bot
        entry_font_size = entry.get("font_size", font_size)
        entry_font = entry.get("font", font_name)
        entry_line_spacing = entry_font_size * 1.3

        c.setFont(entry_font, entry_font_size)

        # Check if text needs wrapping
        text_w = c.stringWidth(text, entry_font, entry_font_size)
        if text_w <= box_w:
            # Single line — vertically centered
            baseline_y = y_bot + (box_h - entry_font_size) / 2 + 2
            c.drawString(x0 + padding, baseline_y, text)
        else:
            # Word-wrap
            words = text.split()
            lines, current = [], ""
            for word in words:
                test = f"{current} {word}".strip()
                if c.stringWidth(test, entry_font, entry_font_size) <= box_w:
                    current = test
                else:
                    if current:
                        lines.append(current)
                    current = word
            if current:
                lines.append(current)

            y_start = y_top - entry_line_spacing
            for i, line in enumerate(lines):
                y = y_start - i * entry_line_spacing
                if y < y_bot:
                    break
                c.drawString(x0 + padding, y, line)

        fields_placed.append(key)

    c.save()
    buf.seek(0)

    # Merge overlay with template
    overlay_reader = PdfReader(buf)
    writer = PdfWriter()

    for i, pg in enumerate(reader.pages):
        target_page_idx = (args.page - 1) if args.page else 0
        if i == target_page_idx:
            pg.merge_page(overlay_reader.pages[0])
        writer.add_page(pg)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as fh:
            writer.write(fh)
    except PermissionError:
        error_exit(f"Permission denied writing to: {output_path}", "PermissionError", 1)
    except OSError as exc:
        error_exit(str(exc), "IOError", 1)

    _json_out({
        "status": "success",
        "output_file": str(output_path),
        "fields_placed": fields_placed,
        "font": font_name,
        "font_size": font_size,
    })


def cmd_info(args):
    pdf_path = validate_pdf_path(args.pdf_path)
    reader = get_reader(pdf_path)

    raw_fields = reader.get_fields()
    has_forms = bool(raw_fields)
    field_count = len(raw_fields) if raw_fields else 0

    _json_out({
        "file": str(pdf_path),
        "file_size_bytes": pdf_path.stat().st_size,
        "page_count": len(reader.pages),
        "has_forms": has_forms,
        "form_field_count": field_count,
        "encrypted": reader.is_encrypted,
        "pdf_version": reader.pdf_header if hasattr(reader, "pdf_header") else None,
    })


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf_tool.py",
        description="CLI for reading and manipulating PDF files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- read-text --
    p_read = subparsers.add_parser("read-text", help="Extract text from a PDF")
    p_read.add_argument("pdf_path", help="Path to the PDF file")
    p_read.add_argument("--pages", default=None, help="Comma-separated 1-based page numbers (e.g. 1,3,5)")
    p_read.add_argument("--format", choices=["text", "json"], default="text", help="Output format (default: text)")
    p_read.set_defaults(func=cmd_read_text)

    # -- metadata --
    p_meta = subparsers.add_parser("metadata", help="Extract PDF metadata")
    p_meta.add_argument("pdf_path", help="Path to the PDF file")
    p_meta.set_defaults(func=cmd_metadata)

    # -- list-fields --
    p_fields = subparsers.add_parser("list-fields", help="List fillable form fields")
    p_fields.add_argument("pdf_path", help="Path to the PDF file")
    p_fields.set_defaults(func=cmd_list_fields)

    # -- fill-form --
    p_fill = subparsers.add_parser("fill-form", help="Fill form fields in a PDF")
    p_fill.add_argument("pdf_path", help="Path to the source PDF")
    p_fill.add_argument("output_path", help="Path for the filled output PDF")
    p_fill.add_argument("--data", default=None, help="JSON string of field_name:value pairs")
    p_fill.add_argument("--data-file", default=None, help="Path to JSON file with field data")
    p_fill.add_argument("--flatten", action="store_true", help="Flatten form fields after filling")
    p_fill.set_defaults(func=cmd_fill_form)

    # -- info --
    p_info = subparsers.add_parser("info", help="Quick PDF probe (page count, forms, size)")
    p_info.add_argument("pdf_path", help="Path to the PDF file")
    p_info.set_defaults(func=cmd_info)

    # -- scan-layout --
    p_scan = subparsers.add_parser("scan-layout", help="Extract form layout (rects, lines, labels) for flat PDFs")
    p_scan.add_argument("pdf_path", help="Path to the PDF file")
    p_scan.add_argument("--pages", default=None, help="Comma-separated 1-based page numbers")
    p_scan.set_defaults(func=cmd_scan_layout)

    # -- overlay-fill --
    p_overlay = subparsers.add_parser("overlay-fill", help="Fill a flat PDF by overlaying text at coordinates")
    p_overlay.add_argument("pdf_path", help="Path to the source PDF (template)")
    p_overlay.add_argument("output_path", help="Path for the filled output PDF")
    p_overlay.add_argument("--data", default=None, help='JSON: {"field": {"text": "value", "box": [x0,y_bot,x1,y_top]}}')
    p_overlay.add_argument("--data-file", default=None, help="Path to JSON file with field data")
    p_overlay.add_argument("--font", default="Helvetica", help="Font name (default: Helvetica)")
    p_overlay.add_argument("--font-size", type=float, default=10, help="Font size in pts (default: 10)")
    p_overlay.add_argument("--padding", type=float, default=4, help="Left padding inside box (default: 4)")
    p_overlay.add_argument("--page", type=int, default=1, help="1-based page number to fill (default: 1)")
    p_overlay.set_defaults(func=cmd_overlay_fill)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()

    if len(sys.argv) < 2:
        parser.print_help(sys.stderr)
        sys.exit(2)

    try:
        args = parser.parse_args()
    except SystemExit as exc:
        # argparse already printed an error message
        sys.exit(2 if exc.code != 0 else 0)

    args.func(args)


if __name__ == "__main__":
    main()
