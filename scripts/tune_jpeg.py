#!/usr/bin/env python3
"""
tune_jpeg.py — find mozjpeg/jpegli quality settings that match benchmark JPEG byte sizes.

For each document, finds large images that both benchmark and v3 converted to JPEG,
extracts original pixels, then tests encoder variants to match benchmark byte sizes.
Scores each result against the original using SSIMULACRA2.

Usage:
    python scripts/tune_jpeg.py [--doc concacaf] [--top 10]
    python scripts/tune_jpeg.py --doc all --top 5
"""

import argparse
import io
import subprocess
import sys
import tempfile
from pathlib import Path
from collections import defaultdict

import pikepdf
from PIL import Image

MOZJPEG  = "/opt/homebrew/opt/mozjpeg/bin/cjpeg"
CJPEGLI  = "/tmp/jpegli/build-codex-fixed/tools/cjpegli"
SSIM2    = "ssimulacra2"

DOCS = {
    "mysports": {
        "original":  "examples/originals/2026-01 MySports - Vesper Proposal.pdf",
        "benchmark": "examples/compressed/2026-01 MySports - Vesper Proposal_benchmark.pdf",
    },
    "company": {
        "original":  "examples/originals/Company Information & Case Studies_Mar 26.pdf",
        "benchmark": "examples/compressed/Company Information & Case Studies_Mar 26_benchmark.pdf",
    },
    "concacaf": {
        "original":  "examples/originals/Concacaf Mobile App Proposal NOV24.pdf",
        "benchmark": "examples/compressed/Concacaf Mobile App Proposal NOV24_benchmark.pdf",
    },
    "deltatre": {
        "original":  "examples/originals/Deltatre x WWE Mobile App Proposal - Dec 2025.pdf",
        "benchmark": "examples/compressed/Deltatre x WWE Mobile App Proposal - Dec 2025_benchmark.pdf",
    },
    "uefa": {
        "original":  "examples/originals/UEFA_OTT Platform Proposal_FINAL_reduced size.pdf",
        "benchmark": "examples/compressed/UEFA_OTT Platform Proposal_Jan 26_benchmark.pdf",
    },
}

MOZJPEG_VARIANTS = [
    ("moz-default",   []),
    ("moz-tune-ssim", ["-tune-ssim"]),
    ("moz-tune-msssim", ["-tune-ms-ssim"]),
    ("moz-1x1",       ["-sample", "1x1"]),
]


# ---------------------------------------------------------------------------
# Filter helpers (same logic as compare_inventories.py)
# ---------------------------------------------------------------------------

def get_filter_names(obj):
    filt = obj.get("/Filter")
    if filt is None:
        return []
    if isinstance(filt, pikepdf.Array):
        return [str(f) for f in filt]
    return [str(filt)]


def is_jpeg_xobj(obj):
    return any("DCT" in n or "JPX" in n for n in get_filter_names(obj))


def is_flat_only(obj):
    names = get_filter_names(obj)
    return bool(names) and all("Flat" in n for n in names)


def get_jpeg_bytes(obj):
    """Return the JPEG bytes from an image XObject.
    Handles both plain /DCTDecode and [/FlateDecode /DCTDecode] chains."""
    names = get_filter_names(obj)
    if len(names) > 1:
        # Outer FlateDecode wrapping JPEG — decode to get JPEG data
        return obj.read_bytes()
    return obj.read_raw_bytes()


# ---------------------------------------------------------------------------
# Image extraction from original PDF
# ---------------------------------------------------------------------------

def extract_pil(obj) -> Image.Image | None:
    """Extract PIL image from a FlateDecode image XObject in the original PDF."""
    try:
        pdfimg = pikepdf.PdfImage(obj)
        pil = pdfimg.as_pil_image()
        # Ensure RGB (not CMYK, not palette) for PPM encoding
        if pil.mode == "CMYK":
            pil = pil.convert("RGB")
        elif pil.mode not in ("RGB", "L"):
            pil = pil.convert("RGB")
        return pil
    except Exception as e:
        # Fallback for edge cases
        try:
            w = int(obj["/Width"])
            h = int(obj["/Height"])
            raw = obj.read_bytes()
            cs = str(obj.get("/ColorSpace", ""))
            if "Gray" in cs:
                mode, bpp = "L", 1
            else:
                mode, bpp = "RGB", 3
            expected = w * h * bpp
            if len(raw) >= expected:
                return Image.frombytes(mode, (w, h), raw[:expected])
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def pil_to_ppm(pil_img: Image.Image) -> bytes:
    buf = io.BytesIO()
    pil_img.save(buf, format="PPM")
    return buf.getvalue()


def encode_mozjpeg(ppm_bytes: bytes, quality: int, extra_flags: list[str]) -> bytes:
    cmd = [MOZJPEG, "-quality", str(quality), "-progressive", "-optimize"] + extra_flags
    result = subprocess.run(cmd, input=ppm_bytes, capture_output=True, check=True)
    return result.stdout


def encode_jpegli(ppm_bytes: bytes, target_bytes: int) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as pf:
        ppm_path = pf.name
        pf.write(ppm_bytes)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as jf:
        jpg_path = jf.name
    try:
        subprocess.run(
            [CJPEGLI, ppm_path, jpg_path, f"--target_size={target_bytes}"],
            check=True, capture_output=True,
        )
        return Path(jpg_path).read_bytes()
    finally:
        Path(ppm_path).unlink(missing_ok=True)
        Path(jpg_path).unlink(missing_ok=True)


def binary_search_mozjpeg(ppm_bytes: bytes, target: int, extra_flags: list[str]) -> tuple[int, bytes]:
    """Find mozjpeg quality whose output byte size is closest to target."""
    lo, hi = 1, 95
    best_q, best_data, best_diff = lo, b"", float("inf")
    while lo <= hi:
        mid = (lo + hi) // 2
        data = encode_mozjpeg(ppm_bytes, mid, extra_flags)
        diff = abs(len(data) - target)
        if diff < best_diff:
            best_diff, best_q, best_data = diff, mid, data
        if len(data) < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return best_q, best_data


# ---------------------------------------------------------------------------
# SSIMULACRA2
# ---------------------------------------------------------------------------

def score_ssim2(ref_pil: Image.Image, candidate_bytes: bytes) -> float:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as rf:
        ref_path = rf.name
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as cf:
        cand_path = cf.name
    try:
        ref_pil.save(ref_path, format="PNG")
        Path(cand_path).write_bytes(candidate_bytes)
        result = subprocess.run(
            [SSIM2, ref_path, cand_path],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    finally:
        Path(ref_path).unlink(missing_ok=True)
        Path(cand_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PDF inventory
# ---------------------------------------------------------------------------

def collect_images(pdf_path: Path) -> list[dict]:
    """Return image XObjects (non-smask), with their dims, encoding, raw bytes, and object."""
    rows = []
    with pikepdf.open(pdf_path) as pdf:
        smask_nums = set()
        for obj in pdf.objects:
            try:
                sm = obj.get("/SMask")
                if sm is not None and hasattr(sm, "objgen"):
                    smask_nums.add(sm.objgen[0])
            except Exception:
                pass

        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            if obj.get("/Subtype") != pikepdf.Name("/Image"):
                continue
            if obj.get("/ImageMask") == pikepdf.Boolean(True):
                continue
            if obj.objgen[0] in smask_nums:
                continue

            w = int(obj.get("/Width", 0))
            h = int(obj.get("/Height", 0))
            if w == 0 or h == 0:
                continue
            bpc = obj.get("/BitsPerComponent")
            bpc = int(bpc) if bpc is not None else 8

            jpeg = is_jpeg_xobj(obj)
            flat = is_flat_only(obj)
            enc = "jpeg" if jpeg else ("flat" if flat else "other")

            try:
                if jpeg:
                    nbytes = len(get_jpeg_bytes(obj))
                else:
                    nbytes = len(obj.read_raw_bytes())
            except Exception:
                nbytes = 0

            rows.append({
                "w": w, "h": h, "bpc": bpc,
                "enc": enc,
                "bytes": nbytes,
                "objgen": obj.objgen,
            })
    return rows


# ---------------------------------------------------------------------------
# Main per-document logic
# ---------------------------------------------------------------------------

def tune_doc(doc_name: str, paths: dict, top_n: int, have_jpegli: bool):
    print(f"\n{'='*72}")
    print(f"  {doc_name.upper()}")
    print(f"{'='*72}")

    orig_rows  = collect_images(Path(paths["original"]))
    bench_rows = collect_images(Path(paths["benchmark"]))

    # Index by (w, h, bpc)
    def idx(rows):
        d = defaultdict(list)
        for r in rows:
            d[(r["w"], r["h"], r["bpc"])].append(r)
        return d

    orig_idx  = idx(orig_rows)
    bench_idx = idx(bench_rows)

    # Find unique-dim images that are flat in original and JPEG in benchmark
    candidates = []
    for dims, o_list in orig_idx.items():
        if dims not in bench_idx:
            continue
        if len(o_list) != 1 or len(bench_idx[dims]) != 1:
            continue  # skip dimension collisions
        o = o_list[0]
        b = bench_idx[dims][0]
        if o["enc"] == "flat" and b["enc"] == "jpeg" and b["bytes"] > 1000:
            candidates.append({
                "dims": dims,
                "orig_bytes": o["bytes"],
                "bench_bytes": b["bytes"],
                "orig_objgen": o["objgen"],
                "bench_objgen": b["objgen"],
            })

    # Sort largest benchmark JPEG first
    candidates.sort(key=lambda x: -x["bench_bytes"])
    candidates = candidates[:top_n]

    if not candidates:
        print("  No suitable candidate images found.")
        return

    print(f"  Testing top {len(candidates)} images (by benchmark JPEG size)\n")

    # Header
    variant_names = [v[0] for v in MOZJPEG_VARIANTS]
    if have_jpegli:
        variant_names.append("jpegli")
    col_w = 14
    hdr = f"  {'Image':>12}  {'bench KB':>8}  {'orig KB':>8}"
    for vn in variant_names:
        hdr += f"  {vn:>{col_w}}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    # Per-image results accumulator
    all_results = []

    with pikepdf.open(Path(paths["original"])) as orig_pdf, \
         pikepdf.open(Path(paths["benchmark"])) as bench_pdf:

        orig_objs  = {obj.objgen: obj for obj in orig_pdf.objects if isinstance(obj, pikepdf.Stream)}
        bench_objs = {obj.objgen: obj for obj in bench_pdf.objects if isinstance(obj, pikepdf.Stream)}

        for cand in candidates:
            dims = cand["dims"]
            w, h, bpc = dims
            target = cand["bench_bytes"]

            orig_obj  = orig_objs.get(cand["orig_objgen"])
            bench_obj = bench_objs.get(cand["bench_objgen"])
            if orig_obj is None or bench_obj is None:
                continue

            pil = extract_pil(orig_obj)
            if pil is None:
                print(f"  {w}×{h}: could not extract original pixels, skipping")
                continue

            ppm = pil_to_ppm(pil)

            # Benchmark score as baseline
            try:
                bench_jpeg = get_jpeg_bytes(bench_obj)
                bench_ssim = score_ssim2(pil, bench_jpeg)
            except Exception as e:
                print(f"  {w}×{h}: benchmark score failed ({e}), skipping")
                continue

            row_label = f"{w}×{h}"
            row_line  = f"  {row_label:>12}  {target//1024:>8}  {cand['orig_bytes']//1024:>8}"

            row_data = {
                "dims": dims,
                "bench_bytes": target,
                "orig_bytes": cand["orig_bytes"],
                "bench_ssim": bench_ssim,
                "variants": {},
            }

            # --- mozjpeg variants ---
            for vname, extra_flags in MOZJPEG_VARIANTS:
                try:
                    q, data = binary_search_mozjpeg(ppm, target, extra_flags)
                    ssim = score_ssim2(pil, data)
                    cell = f"q{q} {len(data)//1024}KB {ssim:+.1f}"
                    row_data["variants"][vname] = {
                        "quality": q, "bytes": len(data), "ssim": ssim,
                        "ssim_delta": ssim - bench_ssim,
                    }
                except Exception as e:
                    cell = f"ERR"
                    row_data["variants"][vname] = None
                row_line += f"  {cell:>{col_w}}"

            # --- jpegli ---
            if have_jpegli:
                try:
                    data = encode_jpegli(ppm, target)
                    ssim = score_ssim2(pil, data)
                    cell = f"{len(data)//1024}KB {ssim:+.1f}"
                    row_data["variants"]["jpegli"] = {
                        "bytes": len(data), "ssim": ssim,
                        "ssim_delta": ssim - bench_ssim,
                    }
                except Exception as e:
                    cell = f"ERR"
                    row_data["variants"]["jpegli"] = None
                row_line += f"  {cell:>{col_w}}"

            print(row_line)
            print(f"  {'':>12}  {'':>8}  {'':>8}  bench: {bench_ssim:.1f} SSIM2")
            all_results.append(row_data)

    # Summary
    if not all_results:
        return
    print(f"\n  Summary — avg SSIM2 delta vs benchmark:")
    totals = defaultdict(list)
    for r in all_results:
        for vname, v in r["variants"].items():
            if v is not None:
                totals[vname].append(v["ssim_delta"])
    for vname in variant_names:
        deltas = totals.get(vname, [])
        if deltas:
            avg = sum(deltas) / len(deltas)
            print(f"    {vname:<20} avg delta vs bench: {avg:+.2f}  (n={len(deltas)})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc",  choices=list(DOCS.keys()) + ["all"], default="concacaf")
    parser.add_argument("--top",  type=int, default=10, help="Images per document")
    args = parser.parse_args()

    # Check for optional jpegli
    have_jpegli = Path(CJPEGLI).is_file()
    if not have_jpegli:
        print(f"Note: cjpegli not found at {CJPEGLI} — jpegli variants will be skipped.", file=sys.stderr)

    doc_list = list(DOCS.items()) if args.doc == "all" else [(args.doc, DOCS[args.doc])]
    for doc_name, paths in doc_list:
        for k, v in paths.items():
            if not Path(v).exists():
                print(f"Missing: {v}", file=sys.stderr)
                sys.exit(1)
        tune_doc(doc_name, paths, args.top, have_jpegli)


if __name__ == "__main__":
    main()
