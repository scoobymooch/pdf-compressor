#!/usr/bin/env python3
"""
compress_pdf.py  —  PDF compressor v4

Strategy: iterate all image XObjects in the PDF; re-encode any that are
FlateDecode (lossless) and 8-bit as JPEG.
Smasks (alpha channels) are also JPEG-compressed at a configurable quality.
PDF structure is compressed using object streams (PDF 1.5+).
16-bit images, already-JPEG images, and tiny images are left unchanged.

Encoders (in preference order):
  jpegli  — best quality/size ratio; uses butteraugli distance metric
  mozjpeg — fast, ~40% smaller than libjpeg-turbo at same quality
  pillow  — fallback; uses system libjpeg-turbo

Usage:
    python scripts/compress_pdf.py input.pdf
    python scripts/compress_pdf.py input.pdf -o output.pdf
    python scripts/compress_pdf.py input.pdf --distance 2.5 -v
    python scripts/compress_pdf.py input.pdf --encoder mozjpeg -q 65
"""

import sys
import io
import subprocess
import argparse
import tempfile
import time
from pathlib import Path

import pikepdf
from PIL import Image


# ── paths & defaults ───────────────────────────────────────────────────────────

def _find_binary(name: str, *candidates: str) -> str:
    """
    Resolve a binary path, in priority order:
      1. Bundled inside a PyInstaller .app (sys._MEIPASS)
      2. COMPRESS_PDF_{NAME} environment variable (e.g. COMPRESS_PDF_CJPEGLI)
      3. Each candidate path in order
      4. Bare name (falls back to PATH lookup at call-time)
    """
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / name
        if bundled.is_file():
            return str(bundled)
    env_key = f"COMPRESS_PDF_{name.upper()}"
    if env_key in os.environ:
        return os.environ[env_key]
    for path in candidates:
        if Path(path).is_file():
            return path
    return name   # rely on PATH

# Default search paths — cover Homebrew (Apple Silicon + Intel) and common build locations.
# Override at runtime via COMPRESS_PDF_CJPEGLI / COMPRESS_PDF_CJPEG env vars.
CJPEGLI = _find_binary(
    "cjpegli",
    "/opt/homebrew/bin/cjpegli",                        # brew install libjxl
    "/usr/local/bin/cjpegli",                           # Intel Homebrew
    "/tmp/jpegli/build-codex-fixed/tools/cjpegli",      # local source build
)
MOZJPEG = _find_binary(
    "cjpeg",
    "/opt/homebrew/opt/mozjpeg/bin/cjpeg",              # brew install mozjpeg (Apple Silicon)
    "/usr/local/opt/mozjpeg/bin/cjpeg",                 # brew install mozjpeg (Intel)
)

DEFAULT_QUALITY   = 65     # for mozjpeg / pillow
DEFAULT_DISTANCE  = 7.0    # for jpegli (butteraugli; 1.0=lossless, higher=more aggressive)
DEFAULT_SMASK_QUALITY = 65 # smask quality (mozjpeg/pillow), or jpegli uses same distance
MIN_PIXELS = 64 * 64       # skip images smaller than ~4 KB raw


# ── filter helpers ─────────────────────────────────────────────────────────────

def get_filter(obj) -> str:
    """Return the first (or only) filter name as a string, or ''."""
    filt = obj.get("/Filter")
    if filt is None:
        return ""
    if isinstance(filt, pikepdf.Array):
        return str(filt[0]) if len(filt) else ""
    return str(filt)


def collect_smask_objnums(pdf) -> set:
    """Return the set of object numbers that are used as /SMask by another image."""
    nums = set()
    for obj in pdf.objects:
        try:
            sm = obj.get("/SMask")
            if sm is not None and hasattr(sm, "objgen"):
                nums.add(sm.objgen[0])
        except Exception:
            pass
    return nums


# ── eligibility checks ─────────────────────────────────────────────────────────

def should_compress(obj, smask_nums, objnum) -> tuple[bool, str]:
    if obj.get("/Subtype") != pikepdf.Name("/Image"):
        return False, "not_image"
    if obj.get("/ImageMask") == pikepdf.Boolean(True):
        return False, "stencil_mask"
    if objnum in smask_nums:
        return False, "smask"
    bpc = obj.get("/BitsPerComponent")
    if bpc is not None and int(bpc) != 8:
        return False, f"{int(bpc)}bpc"
    filt = get_filter(obj)
    if "DCT" in filt or "JPX" in filt:
        return False, "already_jpeg"
    if filt and "Flat" not in filt and "LZW" not in filt:
        return False, f"filter:{filt}"
    w = int(obj.get("/Width", 0))
    h = int(obj.get("/Height", 0))
    if w * h < MIN_PIXELS:
        return False, "too_small"
    return True, "ok"


def should_compress_smask(obj) -> tuple[bool, str]:
    bpc = obj.get("/BitsPerComponent")
    if bpc is not None and int(bpc) != 8:
        return False, f"{int(bpc)}bpc"
    filt = get_filter(obj)
    if "DCT" in filt or "JPX" in filt:
        return False, "already_jpeg"
    if filt and "Flat" not in filt and "LZW" not in filt:
        return False, f"filter:{filt}"
    w = int(obj.get("/Width", 0))
    h = int(obj.get("/Height", 0))
    if w * h < MIN_PIXELS:
        return False, "too_small"
    return True, "ok"


# ── encoders ───────────────────────────────────────────────────────────────────

def _pil_to_ppm_bytes(pil_img: Image.Image) -> bytes:
    buf = io.BytesIO()
    pil_img.save(buf, format="PPM")  # PIL writes PGM for mode L, PPM for RGB
    return buf.getvalue()


def _encode_jpegli(pil_img: Image.Image, distance: float) -> bytes:
    """Encode via jpegli cjpegli binary using butteraugli distance."""
    ppm_bytes = _pil_to_ppm_bytes(pil_img)
    with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as pf:
        pf.write(ppm_bytes)
        ppm_path = pf.name
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as jf:
        jpg_path = jf.name
    try:
        subprocess.run(
            [CJPEGLI, ppm_path, jpg_path, f"--distance={distance:.3f}"],
            check=True, capture_output=True,
        )
        return Path(jpg_path).read_bytes()
    finally:
        Path(ppm_path).unlink(missing_ok=True)
        Path(jpg_path).unlink(missing_ok=True)


def _encode_mozjpeg(pil_img: Image.Image, quality: int) -> bytes:
    """Encode via mozjpeg cjpeg binary."""
    result = subprocess.run(
        [MOZJPEG, "-quality", str(quality), "-progressive", "-optimize"],
        input=_pil_to_ppm_bytes(pil_img),
        capture_output=True, check=True,
    )
    return result.stdout


def _encode_pillow(pil_img: Image.Image, quality: int) -> bytes:
    """Encode via Pillow (libjpeg-turbo)."""
    buf = io.BytesIO()
    pil_img.save(
        buf, format="JPEG", quality=quality, subsampling=0,
        icc_profile=pil_img.info.get("icc_profile"),
    )
    return buf.getvalue()


def encode_as_jpeg(
    obj,
    encoder: str,
    quality: int,
    distance: float,
) -> bytes | None:
    """Decode the PDF image XObject and re-encode as JPEG. Returns bytes or None."""
    try:
        pdf_img = pikepdf.PdfImage(obj)
        pil_img = pdf_img.as_pil_image()
        if pil_img.mode in ("1", "P"):
            pil_img = pil_img.convert("L")
        elif pil_img.mode in ("RGBA", "LA"):
            pil_img = pil_img.convert("RGB")
        elif pil_img.mode == "CMYK":
            pil_img = pil_img.convert("RGB")
        elif pil_img.mode not in ("RGB", "L"):
            pil_img = pil_img.convert("RGB")

        if encoder == "jpegli":
            return _encode_jpegli(pil_img, distance)
        elif encoder == "mozjpeg":
            return _encode_mozjpeg(pil_img, quality)
        else:
            return _encode_pillow(pil_img, quality)
    except Exception:
        return None


def encode_smask_as_jpeg(
    obj,
    encoder: str,
    quality: int,
    distance: float,
) -> bytes | None:
    """Encode a smask (grayscale alpha) as JPEG. Returns bytes or None."""
    try:
        w = int(obj.get("/Width", 0))
        h = int(obj.get("/Height", 0))
        if w == 0 or h == 0:
            return None

        pil_img = None
        try:
            pdf_img = pikepdf.PdfImage(obj)
            pil_img = pdf_img.as_pil_image()
        except Exception:
            pass

        if pil_img is None:
            raw = obj.read_bytes()
            expected = w * h
            if len(raw) < expected:
                return None
            pil_img = Image.frombytes("L", (w, h), raw[:expected])

        if pil_img.mode != "L":
            pil_img = pil_img.convert("L")

        if encoder == "jpegli":
            return _encode_jpegli(pil_img, distance)
        elif encoder == "mozjpeg":
            return _encode_mozjpeg(pil_img, quality)
        else:
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=quality, subsampling=0)
            return buf.getvalue()
    except Exception:
        return None


# ── main compression function ─────────────────────────────────────────────────

def compress_pdf(
    input_path: Path,
    output_path: Path,
    encoder: str = "jpegli",
    quality: int = DEFAULT_QUALITY,
    distance: float = DEFAULT_DISTANCE,
    smask_quality: int = DEFAULT_SMASK_QUALITY,
    verbose: bool = False,
    progress_callback=None,  # callable(done: int, total: int) or None
) -> dict:
    in_size = input_path.stat().st_size

    with pikepdf.open(input_path) as pdf:
        smask_nums = collect_smask_objnums(pdf)

        # Count image streams upfront so caller can show a determinate progress bar
        _prog_total = 0
        _prog_done = 0
        if progress_callback:
            _prog_total = sum(
                1 for o in pdf.objects
                if isinstance(o, pikepdf.Stream)
                and o.get("/Subtype") == pikepdf.Name("/Image")
            )

        stats = {
            "n_images": 0,
            "n_compressed": 0,
            "n_skipped": 0,
            "n_failed": 0,
            "n_smasks": 0,
            "n_smasks_compressed": 0,
            "n_smasks_skipped": 0,
            "n_smasks_failed": 0,
            "bytes_before": 0,
            "bytes_after": 0,
            "smask_bytes_before": 0,
            "smask_bytes_after": 0,
        }
        skip_reasons: dict[str, int] = {}

        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue

            objnum = obj.objgen[0]
            is_smask = objnum in smask_nums

            if progress_callback and obj.get("/Subtype") == pikepdf.Name("/Image"):
                _prog_done += 1
                progress_callback(_prog_done, _prog_total)

            # ── smask path ────────────────────────────────────────────────────
            if is_smask:
                if obj.get("/Subtype") != pikepdf.Name("/Image"):
                    continue

                stats["n_smasks"] += 1
                ok, reason = should_compress_smask(obj)
                if not ok:
                    stats["n_smasks_skipped"] += 1
                    skip_reasons[f"smask:{reason}"] = skip_reasons.get(f"smask:{reason}", 0) + 1
                    continue

                try:
                    orig_bytes = obj.read_raw_bytes()
                except Exception:
                    stats["n_smasks_failed"] += 1
                    continue

                orig_len = len(orig_bytes)
                stats["smask_bytes_before"] += orig_len

                jpeg = encode_smask_as_jpeg(obj, encoder, smask_quality, distance)

                if jpeg is None:
                    stats["n_smasks_failed"] += 1
                    stats["smask_bytes_after"] += orig_len
                    continue

                if len(jpeg) >= orig_len:
                    stats["n_smasks_skipped"] += 1
                    skip_reasons["smask:jpeg_larger"] = skip_reasons.get("smask:jpeg_larger", 0) + 1
                    stats["smask_bytes_after"] += orig_len
                    continue

                obj.write(jpeg, filter=pikepdf.Name.DCTDecode)
                if "/DecodeParms" in obj:
                    del obj["/DecodeParms"]

                stats["smask_bytes_after"] += len(jpeg)
                stats["n_smasks_compressed"] += 1

                if verbose:
                    w = int(obj.get("/Width", 0))
                    h = int(obj.get("/Height", 0))
                    pct = (1 - len(jpeg) / orig_len) * 100
                    print(f"  [smask {objnum:5d}] {w:5d}×{h:<5d}  {orig_len//1024:6d} KB → {len(jpeg)//1024:5d} KB  ({pct:.0f}%)")
                continue

            # ── regular image path ────────────────────────────────────────────
            ok, reason = should_compress(obj, smask_nums, objnum)
            if not ok:
                if reason not in ("not_image", "smask"):
                    stats["n_images"] += 1
                    stats["n_skipped"] += 1
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue

            stats["n_images"] += 1

            try:
                orig_bytes = obj.read_raw_bytes()
            except Exception:
                stats["n_failed"] += 1
                continue

            orig_len = len(orig_bytes)
            stats["bytes_before"] += orig_len

            jpeg = encode_as_jpeg(obj, encoder, quality, distance)

            if jpeg is None:
                if verbose:
                    w = int(obj.get("/Width", 0))
                    h = int(obj.get("/Height", 0))
                    print(f"  [{objnum:5d}] {w}×{h}  ENCODE FAILED")
                stats["n_failed"] += 1
                stats["bytes_after"] += orig_len
                continue

            if len(jpeg) >= orig_len:
                if verbose:
                    w = int(obj.get("/Width", 0))
                    h = int(obj.get("/Height", 0))
                    print(f"  [{objnum:5d}] {w}×{h}  JPEG larger — kept FlateDecode")
                stats["n_skipped"] += 1
                skip_reasons["jpeg_larger"] = skip_reasons.get("jpeg_larger", 0) + 1
                stats["bytes_after"] += orig_len
                continue

            obj.write(jpeg, filter=pikepdf.Name.DCTDecode)
            if "/DecodeParms" in obj:
                del obj["/DecodeParms"]

            stats["bytes_after"] += len(jpeg)
            stats["n_compressed"] += 1

            if verbose:
                w = int(obj.get("/Width", 0))
                h = int(obj.get("/Height", 0))
                pct = (1 - len(jpeg) / orig_len) * 100
                print(f"  [{objnum:5d}] {w:5d}×{h:<5d}  {orig_len//1024:6d} KB → {len(jpeg)//1024:5d} KB  ({pct:.0f}%)")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(output_path, object_stream_mode=pikepdf.ObjectStreamMode.generate)

    out_size = output_path.stat().st_size
    stats["in_mb"]         = in_size / 1e6
    stats["out_mb"]        = out_size / 1e6
    stats["reduction_pct"] = (1 - out_size / in_size) * 100
    stats["skip_reasons"]  = skip_reasons

    total_before = stats["bytes_before"] + stats["smask_bytes_before"]
    total_after  = stats["bytes_after"]  + stats["smask_bytes_after"]
    stats["img_bytes_saved_pct"] = (
        (1 - total_after / total_before) * 100 if total_before else 0
    )
    return stats


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    have_jpegli = Path(CJPEGLI).is_file()
    default_encoder = "jpegli" if have_jpegli else "mozjpeg"

    parser = argparse.ArgumentParser(
        description="compress_pdf v4 — re-encode FlateDecode images as JPEG (jpegli primary)"
    )
    parser.add_argument("input", help="Path to input PDF")
    parser.add_argument("-o", "--output", help="Output path (default: <input>_compressed.pdf)")
    parser.add_argument(
        "--encoder", choices=["jpegli", "mozjpeg", "pillow"], default=default_encoder,
        help=f"JPEG encoder to use (default: {default_encoder})",
    )
    parser.add_argument(
        "--distance", type=float, default=DEFAULT_DISTANCE,
        help=f"jpegli butteraugli distance — lower=better quality (default: {DEFAULT_DISTANCE}). "
             "Ignored for mozjpeg/pillow.",
    )
    parser.add_argument(
        "-q", "--quality", type=int, default=DEFAULT_QUALITY,
        help=f"JPEG quality for mozjpeg/pillow, 1–95 (default: {DEFAULT_QUALITY}). "
             "Ignored for jpegli.",
    )
    parser.add_argument(
        "--smask-quality", type=int, default=DEFAULT_SMASK_QUALITY,
        help=f"JPEG quality for smask fallback (default: {DEFAULT_SMASK_QUALITY})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Print per-image details")
    parser.add_argument("--count-only", action="store_true",
                        help="Print image count and exit (used by GUI for progress bar)")
    args = parser.parse_args()

    if args.count_only:
        import pikepdf as _pk
        with _pk.open(args.input) as _pdf:
            n = sum(
                1 for o in _pdf.objects
                if isinstance(o, _pk.Stream)
                and o.get("/Subtype") == _pk.Name("/Image")
            )
        print(f"image_count:{n}")
        sys.exit(0)

    if args.encoder == "jpegli" and not have_jpegli:
        print(f"Error: jpegli not found at {CJPEGLI}", file=sys.stderr)
        sys.exit(1)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else (
        input_path.parent / (input_path.stem + "_compressed.pdf")
    )

    enc_desc = (
        f"encoder=jpegli distance={args.distance}"
        if args.encoder == "jpegli"
        else f"encoder={args.encoder} quality={args.quality}"
    )
    print(f"\ncompress_pdf v4  ({enc_desc}, smask-quality={args.smask_quality})")
    print(f"  Input : {input_path}  ({input_path.stat().st_size / 1e6:.1f} MB)")
    if args.verbose:
        print()

    t0 = time.time()
    r = compress_pdf(
        input_path, output_path,
        encoder=args.encoder,
        quality=args.quality,
        distance=args.distance,
        smask_quality=args.smask_quality,
        verbose=args.verbose,
    )
    elapsed = time.time() - t0

    if args.verbose:
        print()
    print(f"  Output: {output_path}  ({r['out_mb']:.1f} MB)")
    print(f"  Size  : {r['in_mb']:.1f} MB → {r['out_mb']:.1f} MB  ({r['reduction_pct']:.1f}% reduction)")
    print(
        f"  Images: {r['n_images']} total  |  {r['n_compressed']} compressed"
        f"  |  {r['n_skipped']} skipped  |  {r['n_failed']} failed"
    )
    print(
        f"  Smasks: {r['n_smasks']} total  |  {r['n_smasks_compressed']} compressed"
        f"  |  {r['n_smasks_skipped']} skipped  |  {r['n_smasks_failed']} failed"
    )
    if r["skip_reasons"]:
        print(f"  Skipped: {r['skip_reasons']}")
    print(f"  Time  : {elapsed:.1f}s")


if __name__ == "__main__":
    main()
