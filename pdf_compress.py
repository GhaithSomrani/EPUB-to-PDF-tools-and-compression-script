#!/usr/bin/env python3
"""
pdf_compress.py
---------------
Compress a PDF file using multiple strategies:

  • Re-compress embedded images (JPEG at lower quality / downscale)
  • Remove duplicate objects and unused resources (pypdf)
  • Strip metadata, thumbnails, annotations (optional)
  • Ghostscript pipeline for deep compression (if gs is installed)

Presets:
  --preset screen   →  smallest file, screen viewing  (~72 DPI images)
  --preset ebook    →  balanced default               (~150 DPI images)
  --preset printer  →  high quality print             (~300 DPI images)
  --preset lossless →  no image degradation, structure-only compression

Requirements:
    pip install pypdf pillow pdf2image tqdm

Optional (for best results):
    Ghostscript binary in PATH
      Windows : https://www.ghostscript.com/releases/gsdnld.html
      macOS   : brew install ghostscript
      Linux   : sudo apt install ghostscript

Usage:
    python pdf_compress.py input.pdf output.pdf
    python pdf_compress.py input.pdf output.pdf --preset screen
    python pdf_compress.py input.pdf output.pdf --preset ebook
    python pdf_compress.py input.pdf output.pdf --img-quality 60 --img-dpi 120
    python pdf_compress.py input.pdf output.pdf --no-gs   # skip Ghostscript
"""

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, ArrayObject
except ImportError:
    sys.exit("Missing: pip install pypdf")

try:
    from PIL import Image
except ImportError:
    sys.exit("Missing: pip install pillow")

try:
    from tqdm import tqdm
except ImportError:
    sys.exit("Missing: pip install tqdm")


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS = {
    "screen": dict(
        img_quality=40,
        img_max_dpi=72,
        img_scale=0.5,
        strip_metadata=True,
        strip_annotations=False,
        gs_preset="screen",
    ),
    "ebook": dict(
        img_quality=65,
        img_max_dpi=150,
        img_scale=0.75,
        strip_metadata=True,
        strip_annotations=False,
        gs_preset="ebook",
    ),
    "printer": dict(
        img_quality=85,
        img_max_dpi=300,
        img_scale=1.0,
        strip_metadata=False,
        strip_annotations=False,
        gs_preset="printer",
    ),
    "lossless": dict(
        img_quality=None,   # skip image recompression
        img_max_dpi=None,
        img_scale=1.0,
        strip_metadata=True,
        strip_annotations=False,
        gs_preset=None,
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_mb(n_bytes: int) -> str:
    return f"{n_bytes / 1_048_576:.2f} MB"


def _reduction(before: int, after: int) -> str:
    if before == 0:
        return "N/A"
    pct = (1 - after / before) * 100
    return f"{pct:.1f}% smaller"


# ---------------------------------------------------------------------------
# Strategy 1 – recompress images inside the PDF with pypdf + Pillow
# ---------------------------------------------------------------------------

def _recompress_image(data: bytes, img_quality: int,
                      img_max_dpi: int | None, img_scale: float) -> bytes | None:
    """
    Re-encode a raw image byte-string.
    Returns new JPEG bytes, or None if the image should be kept as-is.
    """
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None   # not an image we can handle

    orig_w, orig_h = img.size

    # Downscale if larger than max_dpi allows  (assume 72 DPI screen baseline)
    if img_max_dpi and max(orig_w, orig_h) > img_max_dpi * 8:
        factor = img_max_dpi / 72.0
        new_w  = max(1, int(orig_w * factor * img_scale))
        new_h  = max(1, int(orig_h * factor * img_scale))
        img    = img.resize((new_w, new_h), Image.LANCZOS)
    elif img_scale < 1.0:
        new_w = max(1, int(orig_w * img_scale))
        new_h = max(1, int(orig_h * img_scale))
        img   = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=img_quality, optimize=True)
    new_data = buf.getvalue()

    # Only replace if we actually made it smaller
    return new_data if len(new_data) < len(data) else None


def compress_images_pypdf(
    reader: PdfReader,
    writer: PdfWriter,
    img_quality: int,
    img_max_dpi: int | None,
    img_scale: float,
) -> int:
    """
    Walk every page's XObject resources, find images, re-compress them.
    Returns count of images recompressed.
    """
    replaced = 0
    pages    = reader.pages

    with tqdm(total=len(pages), desc="  Recompressing images", unit="pg",
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]"
              ) as bar:

        for page in pages:
            resources = page.get("/Resources")
            if not resources:
                bar.update(1)
                continue

            xobjects = resources.get("/XObject")
            if not xobjects:
                bar.update(1)
                continue

            for obj_name in list(xobjects.keys()):
                xobj = xobjects[obj_name]
                if hasattr(xobj, "get_object"):
                    xobj = xobj.get_object()

                if xobj.get("/Subtype") != "/Image":
                    continue

                try:
                    raw = xobj.get_data()
                except Exception:
                    continue

                new_data = _recompress_image(raw, img_quality,
                                             img_max_dpi, img_scale)
                if new_data:
                    xobj._data         = new_data
                    xobj[NameObject("/Filter")]  = NameObject("/DCTDecode")
                    xobj[NameObject("/Length")]  = len(new_data)
                    replaced += 1

            bar.update(1)

    return replaced


# ---------------------------------------------------------------------------
# Strategy 2 – pypdf structural compression (remove duplicates, compress streams)
# ---------------------------------------------------------------------------

def compress_structure(input_path: str, output_path: str,
                       strip_metadata: bool, strip_annotations: bool) -> None:
    """
    Use pypdf to clone the PDF with compression flags set.
    Optionally strips metadata and annotations.
    """
    reader = PdfReader(input_path)
    writer = PdfWriter()

    with tqdm(total=len(reader.pages), desc="  Cloning pages   ",
              unit="pg",
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]"
              ) as bar:
        for page in reader.pages:
            if strip_annotations and "/Annots" in page:
                del page["/Annots"]
            writer.add_page(page)
            bar.update(1)

    if strip_metadata:
        writer.add_metadata({})   # blank metadata

    # Enable compression on all streams
    for page in writer.pages:
        page.compress_content_streams()

    with open(output_path, "wb") as f:
        writer.write(f)


# ---------------------------------------------------------------------------
# Strategy 3 – Ghostscript (optional, best compression)
# ---------------------------------------------------------------------------

GS_PRESETS = {
    "screen":  "/screen",
    "ebook":   "/ebook",
    "printer": "/printer",
    "prepress": "/prepress",
}

def ghostscript_compress(input_path: str, output_path: str,
                         gs_preset: str = "ebook") -> bool:
    """
    Run Ghostscript for deep PDF compression.
    Returns True on success, False if gs is not available.
    """
    gs_bin = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
    if not gs_bin:
        return False

    dPDFSETTINGS = GS_PRESETS.get(gs_preset, "/ebook")

    cmd = [
        gs_bin,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={dPDFSETTINGS}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dEmbedAllFonts=true",
        f"-sOutputFile={output_path}",
        input_path,
    ]

    print(f"  [Ghostscript] running with {dPDFSETTINGS} preset …")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [WARN] Ghostscript error: {result.stderr[:200]}")
        return False
    return True


# ---------------------------------------------------------------------------
# Main compress pipeline
# ---------------------------------------------------------------------------

def compress(
    input_path:        str,
    output_path:       str,
    img_quality:       int   | None = 65,
    img_max_dpi:       int   | None = 150,
    img_scale:         float        = 0.75,
    strip_metadata:    bool         = True,
    strip_annotations: bool         = False,
    use_gs:            bool         = True,
    gs_preset:         str          = "ebook",
) -> None:

    input_path  = str(Path(input_path).resolve())
    output_path = str(Path(output_path).resolve())

    if not os.path.isfile(input_path):
        sys.exit(f"File not found: {input_path}")

    orig_size = os.path.getsize(input_path)
    print(f"\nInput : {input_path}")
    print(f"Size  : {_fmt_mb(orig_size)}")
    print(f"Pages : {len(PdfReader(input_path).pages)}\n")

    with tempfile.TemporaryDirectory(prefix="pdfcompress_") as tmp:
        stage1 = os.path.join(tmp, "stage1.pdf")
        stage2 = os.path.join(tmp, "stage2.pdf")

        # ── Stage 1: structural compression + optional annotation strip ──
        print("[1/3] Structural compression …")
        compress_structure(input_path, stage1,
                           strip_metadata=strip_metadata,
                           strip_annotations=strip_annotations)
        s1_size = os.path.getsize(stage1)
        print(f"      {_fmt_mb(s1_size)}  ({_reduction(orig_size, s1_size)})\n")

        # ── Stage 2: image recompression ─────────────────────────────────
        if img_quality is not None:
            print("[2/3] Image recompression …")
            reader = PdfReader(stage1)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            replaced = compress_images_pypdf(
                reader, writer, img_quality, img_max_dpi, img_scale
            )

            with open(stage2, "wb") as f:
                writer.write(f)

            s2_size = os.path.getsize(stage2)
            print(f"      {replaced} image(s) recompressed")
            print(f"      {_fmt_mb(s2_size)}  ({_reduction(orig_size, s2_size)})\n")
        else:
            shutil.copy(stage1, stage2)
            print("[2/3] Image recompression skipped (lossless preset)\n")

        # ── Stage 3: Ghostscript deep compression ────────────────────────
        print("[3/3] Ghostscript deep compression …")
        gs_ok = False
        if use_gs and gs_preset:
            gs_ok = ghostscript_compress(stage2, output_path, gs_preset)

        if not gs_ok:
            # GS not available or skipped → use stage2 as final output
            shutil.copy(stage2, output_path)
            if use_gs:
                print("      Ghostscript not found — skipping "
                      "(install gs for best results)")
            else:
                print("      Ghostscript skipped (--no-gs)")

    # ── Summary ──────────────────────────────────────────────────────────
    final_size = os.path.getsize(output_path)
    print(f"\n{'─'*50}")
    print(f"  Input  : {_fmt_mb(orig_size)}")
    print(f"  Output : {_fmt_mb(final_size)}")
    print(f"  Saved  : {_fmt_mb(orig_size - final_size)}  "
          f"({_reduction(orig_size, final_size)})")
    print(f"{'─'*50}")
    print(f"\nDone!  →  {output_path}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compress a PDF — images, structure, and optionally Ghostscript.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Presets (override any value with explicit flags):
  --preset screen   →  72 DPI,  quality 40  (smallest, screen only)
  --preset ebook    →  150 DPI, quality 65  (default, good balance)
  --preset printer  →  300 DPI, quality 85  (high quality)
  --preset lossless →  no image changes, structure-only

Examples:
  python pdf_compress.py big.pdf small.pdf
  python pdf_compress.py big.pdf small.pdf --preset screen
  python pdf_compress.py big.pdf small.pdf --preset lossless
  python pdf_compress.py big.pdf small.pdf --img-quality 50 --img-dpi 100
  python pdf_compress.py big.pdf small.pdf --strip-annotations
  python pdf_compress.py big.pdf small.pdf --no-gs
""",
    )
    parser.add_argument("input",  help="Input PDF file")
    parser.add_argument("output", help="Output compressed PDF file")

    parser.add_argument(
        "--preset", choices=PRESETS.keys(), default="ebook",
        help="Compression preset (default: ebook)",
    )
    parser.add_argument(
        "--img-quality", type=int, default=None,
        help="JPEG quality for images 1-100 (overrides preset)",
    )
    parser.add_argument(
        "--img-dpi", type=int, default=None,
        help="Max image DPI — images above this are downscaled (overrides preset)",
    )
    parser.add_argument(
        "--img-scale", type=float, default=None,
        help="Extra scale factor for images 0.1-1.0 (overrides preset)",
    )
    parser.add_argument(
        "--strip-annotations", action="store_true",
        help="Remove all annotations (comments, highlights, links)",
    )
    parser.add_argument(
        "--keep-metadata", action="store_true",
        help="Keep document metadata (author, title, etc.)",
    )
    parser.add_argument(
        "--no-gs", action="store_true",
        help="Skip Ghostscript even if available",
    )
    args = parser.parse_args()

    # Merge preset with any explicit overrides
    cfg = dict(PRESETS[args.preset])
    if args.img_quality    is not None: cfg["img_quality"]    = args.img_quality
    if args.img_dpi        is not None: cfg["img_max_dpi"]    = args.img_dpi
    if args.img_scale      is not None: cfg["img_scale"]      = args.img_scale
    if args.strip_annotations:          cfg["strip_annotations"] = True
    if args.keep_metadata:              cfg["strip_metadata"] = False

    compress(
        args.input,
        args.output,
        img_quality=cfg["img_quality"],
        img_max_dpi=cfg["img_max_dpi"],
        img_scale=cfg["img_scale"],
        strip_metadata=cfg["strip_metadata"],
        strip_annotations=cfg["strip_annotations"],
        use_gs=not args.no_gs,
        gs_preset=cfg.get("gs_preset", "ebook"),
    )


if __name__ == "__main__":
    main()
