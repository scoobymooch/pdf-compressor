# PDF Compressor

A local PDF compression tool for macOS that achieves 85–93% size reduction on design/marketing PDFs — matching or beating online tools like ilovepdf — without uploading your files anywhere.

## How it works

Design tools (Figma, InDesign, Keynote) embed photos as lossless FlateDecode streams. This tool re-encodes those images to JPEG using [jpegli](https://github.com/google/jpegli) (or mozjpeg as fallback), preserving pixel dimensions and colour profiles. No downsampling. No metadata stripping.

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.11+
- [jpegli](https://github.com/google/jpegli) **or** [mozjpeg](https://github.com/mozilla/mozjpeg)
- [pikepdf](https://pikepdf.readthedocs.io/) and [Pillow](https://pillow.readthedocs.io/)

Install Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pikepdf Pillow
```

Install jpegli (recommended) via Homebrew:

```bash
brew install jpegli
```

Or mozjpeg:

```bash
brew install mozjpeg
```

## Usage

### Command line

```bash
python scripts/compress_pdf.py input.pdf
```

Options:

| Flag | Default | Description |
|---|---|---|
| `-o OUTPUT` | `input_compressed.pdf` | Output file path |
| `--distance N` | `7.0` | Butteraugli distance (higher = smaller file, lower quality). Range ~2–15. |
| `--encoder` | `jpegli` | Force encoder: `jpegli` or `mozjpeg` |
| `-v` | off | Verbose: print per-image stats |

Example:

```bash
python scripts/compress_pdf.py report.pdf -o report_small.pdf --distance 7.0 -v
```

### GUI

A drag-and-drop macOS GUI is included:

```bash
python scripts/compress_pdf_gui.py
```

Drop a PDF onto the window, watch the progress bar, then open or reveal the result in Finder.

**Note:** Drag-and-drop requires `tkinterdnd2`. On macOS with Python 3.14+ (Tcl 9), you may need to rebuild the tkdnd native library — see [DEVELOPMENT.md](DEVELOPMENT.md) for details.

## Encoder paths

The tool searches for encoders in this order:

1. Bundled binary (PyInstaller `.app` builds)
2. `COMPRESS_PDF_CJPEGLI` / `COMPRESS_PDF_CJPEG` environment variables
3. Common Homebrew locations (`/opt/homebrew/bin/`, `/usr/local/bin/`)
4. Falls back to Pillow's built-in JPEG encoder

## License

MIT — see [LICENSE](LICENSE).
