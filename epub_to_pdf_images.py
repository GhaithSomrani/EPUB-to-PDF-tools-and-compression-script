#!/usr/bin/env python3
"""
epub_to_pdf_images.py
---------------------
Convert an EPUB to an image-based PDF — pixel-perfect, background images
preserved, and fast thanks to parallel Chromium workers.

Pipeline:
  EPUB  ──►  unzip HTML + all assets to a temp folder
        ──►  N Chromium workers screenshot pages in parallel (ThreadPoolExecutor)
        ──►  ReportLab assembles screenshots into the final PDF

Requirements:
    pip install playwright pillow reportlab pypdf ebooklib
    playwright install chromium

Usage:
    python epub_to_pdf_images.py book.epub book.pdf
    python epub_to_pdf_images.py book.epub book.pdf --scan
    python epub_to_pdf_images.py book.epub book.pdf --pages 1-50
    python epub_to_pdf_images.py book.epub book.pdf --workers 8
    python epub_to_pdf_images.py book.epub book.pdf --dpi 192 --width 1280 --quality 95
"""

import argparse
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
    from reportlab.pdfgen import canvas as rl_canvas
except ImportError:
    sys.exit("Missing: pip install reportlab")

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("Missing: pip install pypdf")

try:
    from playwright.sync_api import sync_playwright, Playwright
except ImportError:
    sys.exit("Missing:\n  pip install playwright\n  playwright install chromium")

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
# Step 1 – unpack EPUB, return ordered list of spine HTML paths
# ---------------------------------------------------------------------------

def unpack_epub(epub_path: str, dest_dir: str) -> list[str]:
    """
    Extract everything from the EPUB zip so that file:// URLs can load
    relative assets (CSS, background images, fonts).
    Returns spine HTML files in reading order.
    """
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
# Step 2 – parallel screenshot rendering
# ---------------------------------------------------------------------------

# Thread-local storage: each worker thread gets its own Playwright + browser
_thread_local = threading.local()

def _get_browser(dpi_scale: float, viewport_width: int):
    """
    Return a (playwright, browser, context) tuple for the current thread,
    creating them lazily on first call.
    Each thread owns its own Chromium instance — no sharing, no locking.
    """
    if not hasattr(_thread_local, "browser"):
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": viewport_width, "height": 800},
            device_scale_factor=dpi_scale,
        )
        _thread_local.pw      = pw
        _thread_local.browser = browser
        _thread_local.context = context
        _thread_local.page    = context.new_page()
    return _thread_local.page


def _render_one(args: tuple) -> tuple[int, str]:
    """
    Worker function: render a single HTML file to a PNG.
    Returns (global_page_index, png_path).
    """
    global_idx, html_file, out_path, dpi_scale, viewport_width = args

    page = _get_browser(dpi_scale, viewport_width)

    url = Path(html_file).as_uri()
    page.goto(url, wait_until="networkidle", timeout=30_000)
    page.wait_for_timeout(300)

    full_height = page.evaluate("() => document.documentElement.scrollHeight")
    if full_height < 10:
        full_height = 1122

    page.set_viewport_size({"width": viewport_width, "height": full_height})
    page.wait_for_timeout(80)

    page.screenshot(path=out_path, full_page=True)
    return global_idx, out_path


def _close_thread_browser():
    """Call at thread shutdown to release the Chromium process."""
    if hasattr(_thread_local, "browser"):
        try:
            _thread_local.browser.close()
            _thread_local.pw.stop()
        except Exception:
            pass


def render_pages_parallel(
    html_files:     list[str],
    out_dir:        str,
    viewport_width: int   = 900,
    dpi_scale:      float = 2.0,
    first_page:     int   = None,   # 1-based inclusive
    last_page:      int   = None,
    workers:        int   = 4,
) -> list[str]:
    """
    Render selected pages in parallel using *workers* Chromium instances.
    Returns a list of PNG paths sorted in page order.
    """
    total    = len(html_files)
    fp_idx   = (first_page or 1) - 1
    lp_idx   = (last_page  or total) - 1
    selected = html_files[fp_idx : lp_idx + 1]
    n        = len(selected)

    # Cap workers to actual page count; no point spawning more browsers
    workers = min(workers, n)

    print(f"  [Playwright] {n} pages  |  {workers} parallel workers  "
          f"|  viewport={viewport_width}px  scale={dpi_scale:.1f}×")

    # Build task list: (global_page_index, html_path, out_png, scale, width)
    tasks = [
        (
            fp_idx + i,
            html_files[fp_idx + i],
            os.path.join(out_dir, f"page_{fp_idx + i + 1:05d}.png"),
            dpi_scale,
            viewport_width,
        )
        for i in range(n)
    ]

    results: dict[int, str] = {}
    start_t = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_render_one, t): t[0] for t in tasks}
        with tqdm(total=n, desc="  Rendering", unit="pg",
                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} "
                             "[{elapsed}<{remaining}, {rate_fmt}]") as bar:
            for fut in as_completed(futures):
                try:
                    idx, path = fut.result()
                    results[idx] = path
                except Exception as exc:
                    tqdm.write(f"  [WARN] page {futures[fut]+1} failed: {exc}")
                bar.update(1)

    # Clean up every browser thread opened
    # (ThreadPoolExecutor reuses threads, so we join them implicitly above)
    for t in threading.enumerate():
        if hasattr(t, "_target"):
            _close_thread_browser()

    # Return pages in original order
    return [results[i] for i in sorted(results)]


# ---------------------------------------------------------------------------
# Step 3 – assemble screenshots into the final PDF
# ---------------------------------------------------------------------------

def images_to_pdf(image_paths: list[str], out_pdf: str, quality: int) -> None:
    print(f"  [ReportLab] assembling {len(image_paths)} pages → {out_pdf} …")

    a4_w = A4[0]
    c    = None

    with tqdm(total=len(image_paths), desc="  Assembling", unit="pg",
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]") as bar:
        for idx, img_path in enumerate(image_paths):
            img    = Image.open(img_path)
            iw, ih = img.size
            scale  = a4_w / iw
            pdf_w  = a4_w
            pdf_h  = ih * scale

            if c is None:
                c = rl_canvas.Canvas(out_pdf, pagesize=(pdf_w, pdf_h))

            c.setPageSize((pdf_w, pdf_h))

            if quality < 100:
                rgb      = img.convert("RGB")
                jpg_path = img_path.replace(".png", "_out.jpg")
                rgb.save(jpg_path, "JPEG", quality=quality, optimize=True)
                draw_src = jpg_path
            else:
                draw_src = img_path

            c.drawImage(draw_src, 0, 0, width=pdf_w, height=pdf_h,
                        preserveAspectRatio=False)
            c.showPage()
            bar.update(1)

    if c:
        c.save()
    print(f"  ✓ Saved: {out_pdf}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def convert(
    epub_path:      str,
    out_pdf:        str,
    dpi:            int  = 150,
    quality:        int  = 85,
    viewport_width: int  = 900,
    workers:        int  = 4,
    page_range:     str  = None,
    scan_only:      bool = False,
) -> None:

    epub_path = str(Path(epub_path).resolve())
    out_pdf   = str(Path(out_pdf).resolve())

    if not os.path.isfile(epub_path):
        sys.exit(f"File not found: {epub_path}")

    dpi_scale = max(1.0, round(dpi / 96.0, 2))

    with tempfile.TemporaryDirectory(prefix="epub2pdf_") as tmp_dir:
        unpack_dir = os.path.join(tmp_dir, "unpacked")
        img_dir    = os.path.join(tmp_dir, "images")
        os.makedirs(unpack_dir)
        os.makedirs(img_dir)

        # ── 1. Unpack ──────────────────────────────────────────────────────
        print("\n[1/3] Unpacking EPUB …")
        html_files = unpack_epub(epub_path, unpack_dir)
        total      = len(html_files)
        print(f"      {total} spine pages found")

        if scan_only:
            print(f"\n── Scan complete ───────────────────────────────────")
            print(f"   Total spine pages : {total}")
            print(f"   Re-run with       : --pages 1-{total}")
            print(f"───────────────────────────────────────────────────\n")
            return

        # ── resolve page range ─────────────────────────────────────────────
        first_page = last_page = None
        if page_range:
            s, e       = parse_page_range(page_range, total)
            first_page = s + 1
            last_page  = e + 1
            n_sel      = last_page - first_page + 1
            print(f"      Exporting pages {first_page}–{last_page} ({n_sel} of {total})")
        else:
            print(f"      Exporting all {total} pages")

        # ── 2. Parallel render ─────────────────────────────────────────────
        print(f"\n[2/3] Rendering with {workers} parallel Chromium workers …")
        t0          = time.time()
        image_paths = render_pages_parallel(
            html_files,
            img_dir,
            viewport_width=viewport_width,
            dpi_scale=dpi_scale,
            first_page=first_page,
            last_page=last_page,
            workers=workers,
        )
        elapsed = time.time() - t0
        print(f"      Done in {elapsed:.1f}s  "
              f"({len(image_paths)/elapsed:.1f} pages/sec)")

        # ── 3. Assemble PDF ────────────────────────────────────────────────
        print("\n[3/3] Building final PDF …")
        images_to_pdf(image_paths, out_pdf, quality)

    print(f"\nDone!  →  {out_pdf}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    # Sensible default: half the logical CPU count, min 2, max 8
    import multiprocessing
    default_workers = max(2, min(8, multiprocessing.cpu_count() // 2))

    parser = argparse.ArgumentParser(
        description="Convert EPUB → image PDF fast via parallel Chromium workers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Speed guide (300-page book, rough estimates):
  --workers 2   ~  5 min
  --workers 4   ~  2-3 min   ← default on your machine ({default_workers})
  --workers 8   ~  1-2 min
  --workers 16  <  1 min     (diminishing returns beyond CPU count)

Examples:
  python epub_to_pdf_images.py book.epub book.pdf
  python epub_to_pdf_images.py book.epub book.pdf --scan
  python epub_to_pdf_images.py book.epub book.pdf --workers 8
  python epub_to_pdf_images.py book.epub book.pdf --pages 1-50
  python epub_to_pdf_images.py book.epub book.pdf --dpi 192 --width 1280 --quality 95
""",
    )
    parser.add_argument("input",  help="Input .epub file")
    parser.add_argument("output", help="Output .pdf file")
    parser.add_argument(
        "--workers", type=int, default=default_workers,
        help=f"Parallel Chromium workers (default: {default_workers} = half your CPU cores)",
    )
    parser.add_argument(
        "--dpi", type=int, default=150,
        help="Render resolution (default 150; 192 = 2× retina-like)",
    )
    parser.add_argument(
        "--quality", type=int, default=85,
        help="JPEG quality 1-100 (default 85; 100 = lossless PNG)",
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
        dpi=args.dpi,
        quality=args.quality,
        viewport_width=args.width,
        workers=args.workers,
        page_range=args.pages,
        scan_only=args.scan,
    )


if __name__ == "__main__":
    main()