#!/usr/bin/env python3
"""
compare_quality.py — side-by-side quality and size comparison of compressed PDFs.

For a given original PDF, extracts large images from the first N pages of:
  - the original (as lossless PNG, for SSIMULACRA2 reference)
  - the benchmark compressed PDF
  - the v3 compressed PDF

Matches images by pixel dimensions, runs SSIMULACRA2 on each matched triple,
and reports SSIMULACRA2 scores and compressed JPEG sizes for each encoder.

Usage:
    python scripts/compare_quality.py --doc mysports --pages 5
    python scripts/compare_quality.py --doc all --pages 3
"""

import argparse
import subprocess
import tempfile
import shutil
import sys
from pathlib import Path
from collections import defaultdict

# ── configuration ─────────────────────────────────────────────────────────────

DOCS = {
    "mysports": {
        "original":  "examples/originals/2026-01 MySports - Vesper Proposal.pdf",
        "benchmark": "examples/compressed/2026-01 MySports - Vesper Proposal_benchmark.pdf",
        "v3":        "tests/output/v3/2026-01 MySports - Vesper Proposal_v3.pdf",
    },
    "company": {
        "original":  "examples/originals/Company Information & Case Studies_Mar 26.pdf",
        "benchmark": "examples/compressed/Company Information & Case Studies_Mar 26_benchmark.pdf",
        "v3":        "tests/output/v3/Company Information & Case Studies_Mar 26_v3.pdf",
    },
    "concacaf": {
        "original":  "examples/originals/Concacaf Mobile App Proposal NOV24.pdf",
        "benchmark": "examples/compressed/Concacaf Mobile App Proposal NOV24_benchmark.pdf",
        "v3":        "tests/output/v3/Concacaf Mobile App Proposal NOV24_v3.pdf",
    },
    "deltatre": {
        "original":  "examples/originals/Deltatre x WWE Mobile App Proposal - Dec 2025.pdf",
        "benchmark": "examples/compressed/Deltatre x WWE Mobile App Proposal - Dec 2025_benchmark.pdf",
        "v3":        "tests/output/v3/Deltatre x WWE Mobile App Proposal - Dec 2025_v3.pdf",
    },
    "uefa": {
        "original":  "examples/originals/UEFA_OTT Platform Proposal_FINAL_reduced size.pdf",
        "benchmark": "examples/compressed/UEFA_OTT Platform Proposal_Jan 26_benchmark.pdf",
        "v3":        "tests/output/v3/UEFA_OTT Platform Proposal_FINAL_reduced size_v3.pdf",
    },
}

MIN_DIMENSION = 500    # ignore images narrower or shorter than this
MIN_JPEG_KB   = 20     # ignore extracted JPEGs smaller than this (thumbnails, icons)


# ── helpers ───────────────────────────────────────────────────────────────────

def extract_images(pdf_path: Path, out_dir: Path, pages: int, lossless: bool):
    """
    Extract images from pages 1..pages of pdf_path into out_dir.
    lossless=True  → -png (decode everything to PNG, for originals)
    lossless=False → -j   (keep JPEGs as JPEG, fall back to PPM for others)
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt_flag = "-png" if lossless else "-j"
    subprocess.run(
        ["pdfimages", fmt_flag, "-f", "1", "-l", str(pages), str(pdf_path),
         str(out_dir / "img")],
        check=True, capture_output=True,
    )


def image_dims(path: Path) -> tuple[int, int] | None:
    """Return (width, height) via ImageMagick identify, or None on failure."""
    try:
        r = subprocess.run(
            ["identify", "-format", "%w %h", str(path)],
            capture_output=True, text=True, check=True,
        )
        w, h = r.stdout.strip().split()
        return int(w), int(h)
    except Exception:
        return None


def ssimulacra2(ref: Path, distorted: Path) -> float | None:
    """Run ssimulacra2 and return the score, or None on failure."""
    try:
        r = subprocess.run(
            ["ssimulacra2", str(ref), str(distorted)],
            capture_output=True, text=True,
        )
        for line in (r.stdout + r.stderr).splitlines():
            line = line.strip()
            try:
                return float(line)
            except ValueError:
                pass
        return None
    except Exception:
        return None


def index_by_dims(directory: Path, min_dim: int) -> dict[tuple, list[Path]]:
    """Return dict: (w,h) → [file, ...] for all images >= min_dim in directory."""
    result = defaultdict(list)
    for f in sorted(directory.iterdir()):
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".ppm"):
            continue
        dims = image_dims(f)
        if dims is None:
            continue
        w, h = dims
        if w >= min_dim and h >= min_dim:
            result[(w, h)].append(f)
    return result


# ── main comparison ───────────────────────────────────────────────────────────

def compare_doc(doc_name: str, paths: dict, pages: int) -> list[dict]:
    """Run comparison for one document. Returns list of result rows."""
    print(f"\n  Extracting images (pages 1–{pages})...", end="", flush=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        orig_dir = tmp / "orig"
        bench_dir = tmp / "bench"
        v3_dir = tmp / "v3"

        # Extract: original as lossless PNG (reference); compressed as JPEG
        extract_images(Path(paths["original"]),  orig_dir,  pages, lossless=True)
        extract_images(Path(paths["benchmark"]), bench_dir, pages, lossless=False)
        extract_images(Path(paths["v3"]),        v3_dir,    pages, lossless=False)

        print(" done.", flush=True)

        # Index by dimensions
        orig_idx  = index_by_dims(orig_dir,  MIN_DIMENSION)
        bench_idx = index_by_dims(bench_dir, MIN_DIMENSION)
        v3_idx    = index_by_dims(v3_dir,    MIN_DIMENSION)

        # Find matching dims present in all three
        common_dims = set(orig_idx) & set(bench_idx) & set(v3_idx)

        rows = []
        for dims in sorted(common_dims, key=lambda d: -(d[0]*d[1])):
            w, h = dims
            orig_file  = orig_idx[dims][0]
            bench_file = bench_idx[dims][0]
            v3_file    = v3_idx[dims][0]

            # Size in KB (the JPEG bytes on disk)
            bench_kb = bench_file.stat().st_size / 1024
            v3_kb    = v3_file.stat().st_size / 1024

            if bench_kb < MIN_JPEG_KB and v3_kb < MIN_JPEG_KB:
                continue

            # SSIMULACRA2 vs original
            bench_ssim = ssimulacra2(orig_file, bench_file)
            v3_ssim    = ssimulacra2(orig_file, v3_file)

            rows.append({
                "doc":      doc_name,
                "dims":     f"{w}×{h}",
                "bench_kb": bench_kb,
                "v3_kb":    v3_kb,
                "bench_ssim": bench_ssim,
                "v3_ssim":    v3_ssim,
            })

        return rows


def print_results(all_rows: list[dict]):
    if not all_rows:
        print("\nNo matching images found.")
        return

    print()
    print(f"{'Document':<12} {'Dimensions':>13}  "
          f"{'Bench KB':>9} {'v3 KB':>7} {'Size ratio':>11}  "
          f"{'Bench SSIM':>11} {'v3 SSIM':>9} {'SSIM Δ':>8}")
    print("-" * 90)

    total_bench_kb = total_v3_kb = 0
    bench_ssims = []
    v3_ssims = []

    for r in all_rows:
        size_ratio = r["v3_kb"] / r["bench_kb"] if r["bench_kb"] > 0 else 0
        ssim_delta = (r["v3_ssim"] - r["bench_ssim"]) if (r["v3_ssim"] and r["bench_ssim"]) else None

        bench_ssim_str = f"{r['bench_ssim']:.2f}" if r["bench_ssim"] else "  N/A"
        v3_ssim_str    = f"{r['v3_ssim']:.2f}"    if r["v3_ssim"]    else "  N/A"
        delta_str      = f"{ssim_delta:+.2f}"      if ssim_delta is not None else "   N/A"

        print(
            f"{r['doc']:<12} {r['dims']:>13}  "
            f"{r['bench_kb']:>9.1f} {r['v3_kb']:>7.1f} {size_ratio:>10.2f}×  "
            f"{bench_ssim_str:>11} {v3_ssim_str:>9} {delta_str:>8}"
        )

        total_bench_kb += r["bench_kb"]
        total_v3_kb    += r["v3_kb"]
        if r["bench_ssim"]: bench_ssims.append(r["bench_ssim"])
        if r["v3_ssim"]:    v3_ssims.append(r["v3_ssim"])

    print("-" * 90)
    n = len(all_rows)
    avg_ratio = total_v3_kb / total_bench_kb if total_bench_kb > 0 else 0
    avg_bench = sum(bench_ssims) / len(bench_ssims) if bench_ssims else 0
    avg_v3    = sum(v3_ssims)    / len(v3_ssims)    if v3_ssims    else 0
    avg_delta = avg_v3 - avg_bench if (avg_bench and avg_v3) else 0

    print(
        f"{'AVERAGE':<12} {'(' + str(n) + ' images)':>13}  "
        f"{total_bench_kb/n:>9.1f} {total_v3_kb/n:>7.1f} {avg_ratio:>10.2f}×  "
        f"{avg_bench:>11.2f} {avg_v3:>9.2f} {avg_delta:>+8.2f}"
    )

    print()
    print("Interpretation:")
    if avg_ratio > 1.0:
        print(f"  v3 images are {(avg_ratio-1)*100:.0f}% larger than benchmark on average.")
    else:
        print(f"  v3 images are {(1-avg_ratio)*100:.0f}% smaller than benchmark on average.")
    if avg_delta > 0:
        print(f"  v3 quality is {avg_delta:+.2f} SSIMULACRA2 points BETTER than benchmark.")
        # Estimate quality adjustment needed to match benchmark size
        # JPEG size scales roughly exponentially with quality; ~each 5 quality steps ≈ 2x size
        # But mozjpeg relationship is non-linear. Rough estimate: 1 quality step ≈ 5% file size change
        approx_q_drop = round((avg_ratio - 1) * 100 / 5) * 5
        print(f"  → Consider lowering quality by ~{approx_q_drop} steps (e.g. q=65 → q={65-approx_q_drop}) "
              f"to match benchmark file size.")
    elif avg_delta < 0:
        print(f"  v3 quality is {avg_delta:.2f} SSIMULACRA2 points WORSE than benchmark.")
        print(f"  → Different compression approach may be needed rather than quality reduction.")
    else:
        print(f"  v3 and benchmark have similar quality.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compare image quality and size: benchmark vs v3"
    )
    parser.add_argument(
        "--doc",
        choices=list(DOCS.keys()) + ["all"],
        default="mysports",
        help="Which document to compare (default: mysports)",
    )
    parser.add_argument(
        "--pages", type=int, default=5,
        help="Number of pages to extract images from (default: 5)",
    )
    args = parser.parse_args()

    doc_list = list(DOCS.items()) if args.doc == "all" else [(args.doc, DOCS[args.doc])]

    all_rows = []
    for doc_name, paths in doc_list:
        print(f"\n{'─'*60}")
        print(f"  {doc_name.upper()}")
        for k, v in paths.items():
            if not Path(v).exists():
                print(f"  ⚠ Missing: {v}", file=sys.stderr)
        rows = compare_doc(doc_name, paths, args.pages)
        print(f"  Found {len(rows)} matching large images")
        all_rows.extend(rows)

    print(f"\n{'='*90}")
    print_results(all_rows)


if __name__ == "__main__":
    main()
