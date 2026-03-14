#!/usr/bin/env python3
"""
compare_inventories.py — compare image encoding across original, benchmark, and v3.

For each document, lists all image XObjects with their dimensions, filter, and
compressed byte size. Matches images across PDFs by dimensions, then reports:
  - Which images the benchmark converted to JPEG that v3 kept as FlateDecode
  - Whether the benchmark's JPEG is larger or smaller than the original FlateDecode
    (this reveals whether the benchmark uses a size guard or not)

Usage:
    python scripts/compare_inventories.py --doc mysports
    python scripts/compare_inventories.py --doc all
"""

import argparse
import sys
from pathlib import Path
from collections import defaultdict

import pikepdf


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


def get_filter(obj) -> str:
    """Return the 'effective' encoding name, scanning all layers of filter chains."""
    filt = obj.get("/Filter")
    if filt is None:
        return "none"
    if isinstance(filt, pikepdf.Array):
        names = [str(f) for f in filt]
    else:
        names = [str(filt)]
    # If any layer is JPEG/JPX, classify as jpeg (the lossy layer dominates)
    for n in names:
        if "DCT" in n or "JPX" in n:
            return n
    return names[0] if names else "none"


def collect_images(pdf_path: Path) -> list[dict]:
    """Return one dict per image XObject (non-smask) with dims, filter, bytes."""
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
            objnum = obj.objgen[0]
            if objnum in smask_nums:
                continue

            w = int(obj.get("/Width", 0))
            h = int(obj.get("/Height", 0))
            if w == 0 or h == 0:
                continue
            bpc = obj.get("/BitsPerComponent")
            bpc = int(bpc) if bpc is not None else 8

            filt = get_filter(obj)

            try:
                if filt in ("/DCTDecode", "/JPXDecode"):
                    # Single JPEG layer — raw bytes IS the JPEG
                    raw_bytes = len(obj.read_raw_bytes())
                elif "DCT" in filt or "JPX" in filt:
                    # Multi-layer (e.g. FlateDecode wrapping JPEG) — read decoded to get JPEG size
                    raw_bytes = len(obj.read_bytes())
                else:
                    raw_bytes = len(obj.read_raw_bytes())
            except Exception:
                raw_bytes = 0
            enc = "jpeg" if ("DCT" in filt or "JPX" in filt) else ("flat" if "Flat" in filt else filt)

            rows.append({
                "w": w, "h": h, "bpc": bpc,
                "enc": enc,
                "bytes": raw_bytes,
            })

    return rows


def index_by_dims(rows: list[dict]) -> dict[tuple, list[dict]]:
    idx = defaultdict(list)
    for r in rows:
        idx[(r["w"], r["h"], r["bpc"])].append(r)
    return idx


def compare_doc(doc_name: str, paths: dict):
    print(f"\n{'='*70}")
    print(f"  {doc_name.upper()}")
    print(f"{'='*70}")

    orig_rows  = collect_images(Path(paths["original"]))
    bench_rows = collect_images(Path(paths["benchmark"]))
    v3_rows    = collect_images(Path(paths["v3"]))

    orig_idx  = index_by_dims(orig_rows)
    bench_idx = index_by_dims(bench_rows)
    v3_idx    = index_by_dims(v3_rows)

    # Summary counts
    def enc_counts(rows):
        jpeg = sum(1 for r in rows if r["enc"] == "jpeg")
        flat = sum(1 for r in rows if r["enc"] == "flat")
        other = len(rows) - jpeg - flat
        total_bytes = sum(r["bytes"] for r in rows)
        return jpeg, flat, other, total_bytes

    oj, of, oo, ob = enc_counts(orig_rows)
    bj, bf, bo, bb = enc_counts(bench_rows)
    vj, vf, vo, vb = enc_counts(v3_rows)

    print(f"  {'':20} {'JPEG':>6} {'Flat':>6} {'other':>6} {'Total MB':>9}")
    print(f"  {'Original':20} {oj:>6} {of:>6} {oo:>6} {ob/1e6:>9.1f}")
    print(f"  {'Benchmark':20} {bj:>6} {bf:>6} {bo:>6} {bb/1e6:>9.1f}")
    print(f"  {'v3':20} {vj:>6} {vf:>6} {vo:>6} {vb/1e6:>9.1f}")

    # Find dims present in all three
    common_dims = set(orig_idx) & set(bench_idx) & set(v3_idx)

    # For each common dimension triple, classify what benchmark and v3 did
    # vs the original
    bench_converted_we_didnt = []   # benchmark=jpeg, v3=flat, orig=flat
    both_converted = []              # benchmark=jpeg, v3=jpeg, orig=flat
    we_converted_bench_didnt = []   # v3=jpeg, benchmark=flat, orig=flat
    bench_jpeg_larger = []           # benchmark converted to JPEG but JPEG > orig flat
    bench_jpeg_smaller = []          # benchmark converted to JPEG and JPEG < orig flat

    for dims in common_dims:
        o = orig_idx[dims][0]
        b = bench_idx[dims][0]
        v = v3_idx[dims][0]

        orig_flat = o["enc"] == "flat"
        bench_jpeg = b["enc"] == "jpeg"
        v3_jpeg = v["enc"] == "jpeg"

        if orig_flat and bench_jpeg and not v3_jpeg:
            bench_converted_we_didnt.append((dims, o["bytes"], b["bytes"], v["bytes"]))
            if b["bytes"] > o["bytes"]:
                bench_jpeg_larger.append((dims, o["bytes"], b["bytes"]))
            else:
                bench_jpeg_smaller.append((dims, o["bytes"], b["bytes"]))

        elif orig_flat and bench_jpeg and v3_jpeg:
            both_converted.append((dims, o["bytes"], b["bytes"], v["bytes"]))

        elif orig_flat and not bench_jpeg and v3_jpeg:
            we_converted_bench_didnt.append((dims, o["bytes"], b["bytes"], v["bytes"]))

    print(f"\n  Image conversion comparison (matched by dimensions, {len(common_dims)} unique dims):")
    print(f"    Both converted orig→JPEG       : {len(both_converted)}")
    print(f"    Benchmark converted, v3 didn't : {len(bench_converted_we_didnt)}")
    print(f"      → Benchmark JPEG < orig flat : {len(bench_jpeg_smaller)}  (benchmark has size guard, JPEG won)")
    print(f"      → Benchmark JPEG > orig flat : {len(bench_jpeg_larger)}  (benchmark has NO size guard)")
    print(f"    v3 converted, benchmark didn't : {len(we_converted_bench_didnt)}")

    # Show the "benchmark converted but we didn't" cases with sizes
    if bench_converted_we_didnt:
        larger = [(d, ob, bb, vb) for d, ob, bb, vb in bench_converted_we_didnt if bb > ob]
        smaller = [(d, ob, bb, vb) for d, ob, bb, vb in bench_converted_we_didnt if bb <= ob]

        if larger:
            print(f"\n  Benchmark JPEG > original FlateDecode (no size guard evidence) — {len(larger)} images:")
            print(f"  {'Dims':>13}  {'Orig flat KB':>13}  {'Bench JPEG KB':>14}  {'Ratio':>7}  {'v3 flat KB':>11}")
            total_orig = total_bench = 0
            for dims, ob_, bb_, vb_ in sorted(larger, key=lambda x: -x[2])[:20]:
                ratio = bb_ / ob_
                print(f"  {str(dims[0])+'×'+str(dims[1]):>13}  {ob_//1024:>13}  {bb_//1024:>14}  {ratio:>7.2f}×  {vb_//1024:>11}")
                total_orig += ob_
                total_bench += bb_
            print(f"  {'TOTAL':>13}  {total_orig//1024:>13}  {total_bench//1024:>14}  {total_bench/total_orig:>7.2f}×")

        if smaller:
            print(f"\n  Benchmark JPEG < original FlateDecode (size guard applied) — {len(smaller)} images:")
            print(f"  {'Dims':>13}  {'Orig flat KB':>13}  {'Bench JPEG KB':>14}  {'Ratio':>7}  {'v3 flat KB':>11}")
            total_orig = total_bench = 0
            for dims, ob_, bb_, vb_ in sorted(smaller, key=lambda x: -x[1])[:20]:
                ratio = bb_ / ob_
                print(f"  {str(dims[0])+'×'+str(dims[1]):>13}  {ob_//1024:>13}  {bb_//1024:>14}  {ratio:>7.2f}×  {vb_//1024:>11}")
                total_orig += ob_
                total_bench += bb_
            print(f"  {'TOTAL':>13}  {total_orig//1024:>13}  {total_bench//1024:>14}  {total_bench/total_orig:>7.2f}×")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", choices=list(DOCS.keys()) + ["all"], default="mysports")
    args = parser.parse_args()

    doc_list = list(DOCS.items()) if args.doc == "all" else [(args.doc, DOCS[args.doc])]
    for doc_name, paths in doc_list:
        for k, v in paths.items():
            if not Path(v).exists():
                print(f"Missing: {v}", file=sys.stderr)
        compare_doc(doc_name, paths)


if __name__ == "__main__":
    main()
