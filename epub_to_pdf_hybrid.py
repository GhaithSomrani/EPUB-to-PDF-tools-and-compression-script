#!/usr/bin/env python3
"""
epub_to_pdf_hybrid.py
---------------------
Convert an EPUB to a compact PDF that keeps background images AND real
selectable/searchable text — dramatically smaller than a pure screenshot PDF.

Strategy per page:
  1. Playwright screenshots the page at LOW resolution → compressed JPEG background
  2. BeautifulSoup extracts the text + approximate positions from the HTML
  3. ReportLab draws the JPEG first, then overlays invisible (opacity=0) text
     so the file is searchable and copy-pasteable

Result:
  • Pure image PDF (epub_to_pdf_images.py)  →  ~150–300 MB for 300 pages
  • This hybrid PDF                          →  ~15–40 MB  for 300 pages

Requirements:
    pip install playwright pillow reportlab pypdf ebooklib beautifulsoup4 lxml tqdm
    playwright install chromium

Usage:
    python epub_to_pdf_hybrid.py book.epub book.pdf
    python epub_to_pdf_hybrid.py book.epub book.pdf --scan
    python epub_to_pdf_hybrid.py book.epub book.pdf --pages 1-50
    python epub_to_pdf_hybrid.py book.epub book.pdf --workers 8
    python epub_to_pdf_hybrid.py book.epub book.pdf --img-quality 60 --img-scale 0.6
"""

import argparse
import io
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
try:
    import ebooklib
    from ebooklib import epub
except ImportError:
    sys.exit("Missing: pip install ebooklib")

try:
    from PIL import Image
except ImportError:
    sys.exit("Missing: pip install pillow")

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
except ImportError:
    sys.exit("Missing: pip install reportlab")

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("Missing: pip install pypdf")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Missing:\n  pip install playwright\n  playwright install chromium")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing: pip install beautifulsoup4 lxml")

try:
    from tqdm import tqdm
except ImportError:
    sys.exit("Missing: pip install tqdm")


# ---------------------------------------------------------------------------
# Page range helpers
# ---------------------------------------------------------------------------

def parse_page_range(spec: str, total: int) -> tuple[int, int]:
    spec = spec.strip()
    if "-" in spec:
        parts   = spec.split("-", 1)
        start_s = parts[0].strip()
        end_s   = parts[1].strip()
        start   = (int(start_s) - 1) if start_s else 0
        end     = (int(end_s)   - 1) if end_s   else total - 1
    else:
        start = end = int(spec) - 1
    start = max(0, min(start, total - 1))
    end   = max(0, min(end,   total - 1))
    if start > end:
        sys.exit(f"Invalid range '{spec}': start ({start+1}) > end ({end+1})")
    return start, end


# ---------------------------------------------------------------------------
# Step 1 – unpack EPUB
# ---------------------------------------------------------------------------

def unpack_epub(epub_path: str, dest_dir: str) -> list[str]:
    """Extract EPUB zip and return spine HTML paths in reading order."""
    with zipfile.ZipFile(epub_path, "r") as zf:
        zf.extractall(dest_dir)

    book  = epub.read_epub(epub_path)
    spine = []
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None:
            continue
        name = item.get_name()
        candidates = [Path(dest_dir) / name] + list(
            Path(dest_dir).rglob(Path(name).name)
        )
        for c in candidates:
            if c.exists():
                spine.append(str(c))
                break

    if not spine:
        spine = sorted(str(p) for p in Path(dest_dir).rglob("*.xhtml")) + \
                sorted(str(p) for p in Path(dest_dir).rglob("*.html"))
    return spine


# ---------------------------------------------------------------------------
# Step 2 – parallel worker: screenshot + extract text in one browser visit
# ---------------------------------------------------------------------------

_thread_local = threading.local()

def _get_page(viewport_width: int, dpi_scale: float):
    """Lazily create a thread-local Playwright browser page."""
    if not hasattr(_thread_local, "page"):
        pw      = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": viewport_width, "height": 800},
            device_scale_factor=dpi_scale,
        )
        _thread_local.pw      = pw
        _thread_local.browser = browser
        _thread_local.page    = context.new_page()
    return _thread_local.page


def _extract_text_blocks(html_content: str) -> list[dict]:
    """
    Parse HTML and return text blocks with rough relative positions.
    Each block: {text, x_pct, y_pct, font_size}
    Positions are 0.0–1.0 fractions of page width/height.
    """
    soup   = BeautifulSoup(html_content, "lxml")
    blocks = []

    # Remove script/style noise
    for tag in soup(["script", "style", "head"]):
        tag.decompose()

    # Walk block-level tags in document order
    tags = soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6",
                           "li", "td", "th", "div", "span", "a"])
    total = max(len(tags), 1)

    for i, tag in enumerate(tags):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) < 2:
            continue

        # Estimate vertical position by document order
        y_pct = i / total

        # Rough font size by tag type
        font_size = {
            "h1": 18, "h2": 15, "h3": 13, "h4": 12,
        }.get(tag.name, 10)

        blocks.append({
            "text":      text,
            "x_pct":     0.05,    # small left margin
            "y_pct":     y_pct,
            "font_size": font_size,
        })

    return blocks


def _render_one_hybrid(args: tuple) -> tuple[int, bytes, list[dict], int, int]:
    """
    Worker: load page, take low-res screenshot, extract text blocks.
    Returns (global_idx, jpeg_bytes, text_blocks, img_w, img_h).
    """
    (global_idx, html_file, viewport_width, dpi_scale,
     img_quality, img_scale) = args

    page = _get_page(viewport_width, dpi_scale)
    url  = Path(html_file).as_uri()

    page.goto(url, wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(300)

    full_height = page.evaluate("() => document.documentElement.scrollHeight")
    if full_height < 10:
        full_height = 1122

    page.set_viewport_size({"width": viewport_width, "height": full_height})
    page.wait_for_timeout(80)

    # Screenshot as PNG bytes (in memory, no disk write)
    png_bytes = page.screenshot(full_page=True)

    # Re-open with Pillow, downscale, save as JPEG for size reduction
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if img_scale < 1.0:
        new_w = max(1, int(img.width  * img_scale))
        new_h = max(1, int(img.height * img_scale))
        img   = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=img_quality, optimize=True)
    jpeg_bytes = buf.getvalue()

    # Extract text from the HTML on disk (not the rendered DOM)
    with open(html_file, "r", encoding="utf-8", errors="replace") as f:
        html_content = f.read()
    text_blocks = _extract_text_blocks(html_content)

    return global_idx, jpeg_bytes, text_blocks, img.width, img.height


def render_pages_parallel(
    html_files:     list[str],
    viewport_width: int   = 900,
    dpi_scale:      float = 1.5,
    img_quality:    int   = 65,
    img_scale:      float = 0.75,
    first_page:     int   = None,
    last_page:      int   = None,
    workers:        int   = 4,
) -> list[tuple]:
    """
    Render pages in parallel.
    Returns list of (jpeg_bytes, text_blocks, img_w, img_h) in page order.
    """
    total    = len(html_files)
    fp_idx   = (first_page or 1) - 1
    lp_idx   = (last_page  or total) - 1
    selected = html_files[fp_idx : lp_idx + 1]
    n        = len(selected)
    workers  = min(workers, n)

    print(f"  [Playwright] {n} pages | {workers} workers | "
          f"viewport={viewport_width}px | scale={dpi_scale:.1f}× | "
          f"img_quality={img_quality} img_scale={img_scale}")

    tasks = [
        (fp_idx + i, html_files[fp_idx + i],
         viewport_width, dpi_scale, img_quality, img_scale)
        for i in range(n)
    ]

    results: dict[int, tuple] = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_render_one_hybrid, t): t[0] for t in tasks}
        with tqdm(total=n, desc="  Rendering", unit="pg",
                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} "
                             "[{elapsed}<{remaining}, {rate_fmt}]") as bar:
            for fut in as_completed(futures):
                try:
                    global_idx, jpeg_bytes, blocks, iw, ih = fut.result()
                    results[global_idx] = (jpeg_bytes, blocks, iw, ih)
                except Exception as exc:
                    tqdm.write(f"  [WARN] page failed: {exc}")
                bar.update(1)

    return [results[fp_idx + i] for i in range(n) if (fp_idx + i) in results]


# ---------------------------------------------------------------------------
# Step 3 – assemble hybrid PDF (JPEG background + invisible text overlay)
# ---------------------------------------------------------------------------

def assemble_hybrid_pdf(
    pages:   list[tuple],
    out_pdf: str,
) -> None:
    """
    For each page: draw JPEG background, then overlay invisible text
    so the PDF is searchable and copy-pasteable at a fraction of the size.
    """
    print(f"  [ReportLab] assembling {len(pages)} hybrid pages → {out_pdf} …")

    a4_w = A4[0]
    c    = None

    with tqdm(total=len(pages), desc="  Assembling", unit="pg",
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} "
                         "[{elapsed}<{remaining}]") as bar:

        for jpeg_bytes, text_blocks, iw, ih in pages:
            # Scale to A4 width keeping aspect ratio
            scale  = a4_w / iw
            pdf_w  = a4_w
            pdf_h  = ih * scale

            if c is None:
                c = rl_canvas.Canvas(out_pdf, pagesize=(pdf_w, pdf_h))

            c.setPageSize((pdf_w, pdf_h))

            # ── background: compressed JPEG ──────────────────────────────
            img_reader = ImageReader(io.BytesIO(jpeg_bytes))
            c.drawImage(img_reader, 0, 0, width=pdf_w, height=pdf_h,
                        preserveAspectRatio=False)

            # ── text overlay: invisible but selectable ───────────────────
            c.saveState()
            c.setFillColorRGB(0, 0, 0, alpha=0)   # fully transparent text

            for block in text_blocks:
                x = block["x_pct"] * pdf_w
                # PDF y=0 is bottom; text_blocks y_pct=0 is top
                y = pdf_h - block["y_pct"] * pdf_h - block["font_size"]
                y = max(2, y)

                fs = block["font_size"]
                c.setFont("Helvetica", fs)

                # Clip text to page width
                max_chars = max(1, int((pdf_w * 0.9) / (fs * 0.55)))
                text      = block["text"][:max_chars]

                try:
                    c.drawString(x, y, text)
                except Exception:
                    pass   # skip unencodable characters silently

            c.restoreState()
            c.showPage()
            bar.update(1)

    if c:
        c.save()
    print(f"  ✓ Saved: {out_pdf}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def convert(
    epub_path:      str,
    out_pdf:        str,
    viewport_width: int   = 900,
    dpi_scale:      float = 1.5,
    img_quality:    int   = 65,
    img_scale:      float = 0.75,
    workers:        int   = 4,
    page_range:     str   = None,
    scan_only:      bool  = False,
) -> None:

    epub_path = str(Path(epub_path).resolve())
    out_pdf   = str(Path(out_pdf).resolve())

    if not os.path.isfile(epub_path):
        sys.exit(f"File not found: {epub_path}")

    with tempfile.TemporaryDirectory(prefix="epub2hybrid_") as tmp_dir:
        unpack_dir = os.path.join(tmp_dir, "unpacked")
        os.makedirs(unpack_dir)

        print("\n[1/3] Unpacking EPUB …")
        html_files = unpack_epub(epub_path, unpack_dir)
        total      = len(html_files)
        print(f"      {total} spine pages found")

        if scan_only:
            print(f"\n── Scan complete ───────────────────────────────────")
            print(f"   Total pages : {total}")
            print(f"   Re-run with : --pages 1-{total}")
            print(f"───────────────────────────────────────────────────\n")
            return

        first_page = last_page = None
        if page_range:
            s, e       = parse_page_range(page_range, total)
            first_page = s + 1
            last_page  = e + 1
            print(f"      Exporting pages {first_page}–{last_page} "
                  f"({last_page - first_page + 1} of {total})")
        else:
            print(f"      Exporting all {total} pages")

        print(f"\n[2/3] Rendering pages (JPEG background + text extraction) …")
        t0    = time.time()
        pages = render_pages_parallel(
            html_files,
            viewport_width=viewport_width,
            dpi_scale=dpi_scale,
            img_quality=img_quality,
            img_scale=img_scale,
            first_page=first_page,
            last_page=last_page,
            workers=workers,
        )
        elapsed = time.time() - t0
        print(f"      Done in {elapsed:.1f}s ({len(pages)/elapsed:.1f} pg/s)")

        print(f"\n[3/3] Building hybrid PDF …")
        assemble_hybrid_pdf(pages, out_pdf)

    size_mb = os.path.getsize(out_pdf) / 1_048_576
    print(f"\nDone!  →  {out_pdf}  ({size_mb:.1f} MB)\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    default_workers = max(2, min(8, multiprocessing.cpu_count() // 2))

    parser = argparse.ArgumentParser(
        description="Convert EPUB → compact hybrid PDF (JPEG bg + real text).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Size vs quality knobs:
  --img-quality 40  --img-scale 0.5   →  smallest file  (~5–10 MB / 300 pages)
  --img-quality 65  --img-scale 0.75  →  default balance (~15–25 MB)
  --img-quality 85  --img-scale 1.0   →  best quality    (~60–80 MB)

Examples:
  python epub_to_pdf_hybrid.py book.epub book.pdf
  python epub_to_pdf_hybrid.py book.epub book.pdf --scan
  python epub_to_pdf_hybrid.py book.epub book.pdf --pages 1-50
  python epub_to_pdf_hybrid.py book.epub book.pdf --workers 8
  python epub_to_pdf_hybrid.py book.epub book.pdf --img-quality 50 --img-scale 0.6
""",
    )
    parser.add_argument("input",  help="Input .epub file")
    parser.add_argument("output", help="Output .pdf file")
    parser.add_argument(
        "--workers", type=int, default=default_workers,
        help=f"Parallel Chromium workers (default: {default_workers})",
    )
    parser.add_argument(
        "--img-quality", type=int, default=65,
        help="JPEG quality for background images 1-100 (default 65)",
    )
    parser.add_argument(
        "--img-scale", type=float, default=0.75,
        help="Downscale factor for background images 0.1-1.0 (default 0.75)",
    )
    parser.add_argument(
        "--dpi-scale", type=float, default=1.5,
        help="Chromium device pixel ratio (default 1.5)",
    )
    parser.add_argument(
        "--width", type=int, default=900,
        help="Viewport width in CSS pixels (default 900)",
    )
    parser.add_argument(
        "--pages", metavar="RANGE",
        help="Page range: '1-50', '10-', '-30', or '7'",
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Count pages and exit without writing a PDF",
    )
    args = parser.parse_args()

    convert(
        args.input, args.output,
        viewport_width=args.width,
        dpi_scale=args.dpi_scale,
        img_quality=args.img_quality,
        img_scale=args.img_scale,
        workers=args.workers,
        page_range=args.pages,
        scan_only=args.scan,
    )


if __name__ == "__main__":
    main()
