# EPUB & PDF Tools

Three Python command-line tools for converting EPUBs to PDF and compressing PDFs.

## Tools

### `epub_to_pdf_hybrid.py` — EPUB → compact searchable PDF
Converts an EPUB to a PDF with JPEG background images and an invisible text overlay — searchable and copy-pasteable at a fraction of the size of a pure image PDF.

| Mode | File size (300 pages) |
|------|-----------------------|
| Pure image PDF (`epub_to_pdf_images.py`) | ~150–300 MB |
| **Hybrid PDF (this script)** | **~15–40 MB** |

**Requirements**
```
pip install playwright pillow reportlab pypdf ebooklib beautifulsoup4 lxml tqdm
playwright install chromium
```

**Usage**
```bash
python epub_to_pdf_hybrid.py book.epub book.pdf
python epub_to_pdf_hybrid.py book.epub book.pdf --scan           # count pages
python epub_to_pdf_hybrid.py book.epub book.pdf --pages 1-50
python epub_to_pdf_hybrid.py book.epub book.pdf --workers 8
python epub_to_pdf_hybrid.py book.epub book.pdf --img-quality 60 --img-scale 0.6
```

| Flag | Default | Description |
|------|---------|-------------|
| `--workers` | half CPU cores | Parallel Chromium workers |
| `--img-quality` | 65 | JPEG quality (1–100) |
| `--img-scale` | 0.75 | Background image downscale factor |
| `--dpi-scale` | 1.5 | Chromium device pixel ratio |
| `--width` | 900 | Viewport width (CSS px) |
| `--pages` | all | Range: `1-50`, `10-`, `-30`, or `7` |
| `--scan` | — | Print page count and exit |

---

### `epub_to_pdf_images.py` — EPUB → pixel-perfect image PDF
Converts every EPUB page to a screenshot and assembles them into a PDF. No text layer — purely visual, highest fidelity.

**Requirements**
```
pip install playwright pillow reportlab pypdf ebooklib tqdm
playwright install chromium
```

**Usage**
```bash
python epub_to_pdf_images.py book.epub book.pdf
python epub_to_pdf_images.py book.epub book.pdf --scan
python epub_to_pdf_images.py book.epub book.pdf --workers 8
python epub_to_pdf_images.py book.epub book.pdf --pages 1-50
python epub_to_pdf_images.py book.epub book.pdf --dpi 192 --width 1280 --quality 95
```

| Flag | Default | Description |
|------|---------|-------------|
| `--workers` | half CPU cores | Parallel Chromium workers |
| `--dpi` | 150 | Render resolution |
| `--quality` | 85 | JPEG quality (100 = lossless PNG) |
| `--width` | 900 | Viewport width (CSS px) |
| `--pages` | all | Range: `1-50`, `10-`, `-30`, or `7` |
| `--scan` | — | Print page count and exit |

---

### `pdf_compress.py` — PDF compressor
Compresses an existing PDF through three stages: structural compression (pypdf), image recompression (Pillow/JPEG), and optional Ghostscript deep compression.

**Requirements**
```
pip install pypdf pillow tqdm
```
Optional — for best results:
```
# Windows: https://www.ghostscript.com/releases/gsdnld.html
# macOS:   brew install ghostscript
# Linux:   sudo apt install ghostscript
```

**Presets**

| Preset | DPI | JPEG quality | Use case |
|--------|-----|-------------|----------|
| `screen` | 72 | 40 | Smallest file, screen viewing |
| `ebook` *(default)* | 150 | 65 | Balanced |
| `printer` | 300 | 85 | High-quality print |
| `lossless` | — | — | Structure-only, no image changes |

**Usage**
```bash
python pdf_compress.py input.pdf output.pdf
python pdf_compress.py input.pdf output.pdf --preset screen
python pdf_compress.py input.pdf output.pdf --preset lossless
python pdf_compress.py input.pdf output.pdf --img-quality 50 --img-dpi 100
python pdf_compress.py input.pdf output.pdf --strip-annotations
python pdf_compress.py input.pdf output.pdf --no-gs
```

| Flag | Description |
|------|-------------|
| `--preset` | Compression preset (default: `ebook`) |
| `--img-quality` | JPEG quality override |
| `--img-dpi` | Max image DPI override |
| `--img-scale` | Extra scale factor override |
| `--strip-annotations` | Remove comments, highlights, links |
| `--keep-metadata` | Preserve document metadata |
| `--no-gs` | Skip Ghostscript |

## .gitignore
`*.epub`, `*.pdf`, and `venv/` are excluded from version control.
