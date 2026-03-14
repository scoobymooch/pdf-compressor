#!/usr/bin/env python3
"""
analyze_resampling.py — estimate potential savings from image downsampling.

For each image XObject, finds all page placements, computes the effective
display PPI (via the current transformation matrix at each Do call), and
estimates how much file size could be saved by downsampling to a target DPI.

Usage:
    python scripts/analyze_resampling.py examples/originals/*.pdf
    python scripts/analyze_resampling.py --target-dpi 150 examples/originals/foo.pdf
"""

import math
import argparse
import sys
from pathlib import Path
from collections import defaultdict

import pikepdf


# ── matrix helpers ─────────────────────────────────────────────────────────────

def mat_mul(m, n):
    """Multiply two 6-element PDF CTMs [a b c d e f] (row-major)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return [
        a1*a2 + b1*c2,
        a1*b2 + b1*d2,
        c1*a2 + d1*c2,
        c1*b2 + d1*d2,
        e1*a2 + f1*c2 + e2,
        e1*b2 + f1*d2 + f2,
    ]

IDENTITY = [1, 0, 0, 1, 0, 0]

def display_size_pts(ctm):
    """Return (width_pts, height_pts) of a unit square after CTM transform."""
    a, b, c, d, e, f = ctm
    w = math.sqrt(a*a + b*b)
    h = math.sqrt(c*c + d*d)
    return w, h


# ── XObject image resolution analysis ─────────────────────────────────────────

def collect_image_placements(pdf):
    """
    Walk all pages and (one level of) form XObjects to find every image
    placement. Returns dict: objnum → list of (display_w_pts, display_h_pts).
    """
    placements = defaultdict(list)  # objnum → [(w_pts, h_pts), ...]

    def walk_content(resources, content_obj, ctm_stack):
        try:
            instructions = pikepdf.parse_content_stream(content_obj)
        except Exception:
            return

        ctm = ctm_stack[-1][:]

        for operands, operator in instructions:
            op = str(operator)

            if op == "q":
                ctm_stack.append(ctm[:])

            elif op == "Q":
                if len(ctm_stack) > 1:
                    ctm_stack.pop()
                    ctm = ctm_stack[-1][:]

            elif op == "cm":
                try:
                    m = [float(o) for o in operands]
                    ctm = mat_mul(m, ctm)
                    ctm_stack[-1] = ctm[:]
                except Exception:
                    pass

            elif op == "Do":
                try:
                    name = str(operands[0])
                    xobj = resources.get("/XObject", {}).get(name)
                    if xobj is None:
                        continue
                    subtype = xobj.get("/Subtype")

                    if subtype == pikepdf.Name("/Image"):
                        w_pts, h_pts = display_size_pts(ctm)
                        objnum = xobj.objgen[0]
                        placements[objnum].append((w_pts, h_pts))

                    elif subtype == pikepdf.Name("/Form"):
                        # Recurse into form XObject (one level)
                        sub_resources = xobj.get("/Resources", resources)
                        sub_ctm_raw = xobj.get("/Matrix")
                        if sub_ctm_raw is not None:
                            sub_m = [float(v) for v in sub_ctm_raw]
                            sub_ctm = mat_mul(sub_m, ctm)
                        else:
                            sub_ctm = ctm[:]
                        walk_content(sub_resources, xobj, [sub_ctm])

                except Exception:
                    pass

    for page in pdf.pages:
        resources = page.get("/Resources", {})
        walk_content(resources, page, [IDENTITY[:]])

    return placements


def analyze_pdf(pdf_path: Path, target_dpi: float) -> dict:
    """
    Analyse one PDF. Returns summary dict.
    """
    with pikepdf.open(pdf_path) as pdf:
        # Collect smask objnums to skip (smasks have no independent display size)
        smask_nums = set()
        for obj in pdf.objects:
            try:
                sm = obj.get("/SMask")
                if sm is not None and hasattr(sm, "objgen"):
                    smask_nums.add(sm.objgen[0])
            except Exception:
                pass

        placements = collect_image_placements(pdf)

        # Build per-image stats
        rows = []
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            if obj.get("/Subtype") != pikepdf.Name("/Image"):
                continue
            objnum = obj.objgen[0]
            if objnum in smask_nums:
                continue

            px_w = int(obj.get("/Width", 0))
            px_h = int(obj.get("/Height", 0))
            if px_w == 0 or px_h == 0:
                continue

            # Compressed stream size (bytes as stored in PDF)
            try:
                raw_bytes = len(obj.read_raw_bytes())
            except Exception:
                raw_bytes = 0

            # Find maximum display size across all placements
            uses = placements.get(objnum, [])
            if not uses:
                # Image not found in page content — skip for resampling analysis
                rows.append({
                    "objnum": objnum,
                    "px_w": px_w, "px_h": px_h,
                    "raw_bytes": raw_bytes,
                    "max_display_w_pts": None,
                    "max_display_h_pts": None,
                    "effective_dpi": None,
                    "n_uses": 0,
                    "can_downsample": False,
                    "target_px_w": None,
                    "target_px_h": None,
                    "downsample_ratio": None,
                    "estimated_saving_bytes": 0,
                })
                continue

            # Use the largest placement (most demanding use case)
            max_w_pts = max(w for w, h in uses)
            max_h_pts = max(h for w, h in uses)

            # Effective PPI at largest use
            eff_dpi_w = px_w / (max_w_pts / 72) if max_w_pts > 0 else 0
            eff_dpi_h = px_h / (max_h_pts / 72) if max_h_pts > 0 else 0
            eff_dpi = max(eff_dpi_w, eff_dpi_h)  # conservative: use whichever axis has higher res

            # Can we downsample?
            can_ds = eff_dpi > target_dpi * 1.1  # need at least 10% excess to bother
            if can_ds:
                ratio = target_dpi / eff_dpi
                target_px_w = max(1, round(px_w * ratio))
                target_px_h = max(1, round(px_h * ratio))
                # JPEG file size scales roughly with pixel count (linear with area)
                area_ratio = (target_px_w * target_px_h) / (px_w * px_h)
                estimated_saving_bytes = int(raw_bytes * (1 - area_ratio))
            else:
                ratio = None
                target_px_w = target_px_h = None
                estimated_saving_bytes = 0

            rows.append({
                "objnum": objnum,
                "px_w": px_w, "px_h": px_h,
                "raw_bytes": raw_bytes,
                "max_display_w_pts": max_w_pts,
                "max_display_h_pts": max_h_pts,
                "effective_dpi": eff_dpi,
                "n_uses": len(uses),
                "can_downsample": can_ds,
                "target_px_w": target_px_w,
                "target_px_h": target_px_h,
                "downsample_ratio": ratio,
                "estimated_saving_bytes": estimated_saving_bytes,
            })

        total_raw = sum(r["raw_bytes"] for r in rows)
        downsample_candidates = [r for r in rows if r["can_downsample"]]
        total_saving = sum(r["estimated_saving_bytes"] for r in rows)

        return {
            "path": pdf_path,
            "file_size_mb": pdf_path.stat().st_size / 1e6,
            "n_images": len(rows),
            "n_placed": sum(1 for r in rows if r["n_uses"] > 0),
            "n_candidates": len(downsample_candidates),
            "total_image_raw_bytes": total_raw,
            "estimated_saving_bytes": total_saving,
            "rows": rows,
        }


# ── reporting ──────────────────────────────────────────────────────────────────

def print_report(result: dict, target_dpi: float, verbose: bool):
    path = result["path"]
    file_mb = result["file_size_mb"]
    saving_mb = result["estimated_saving_bytes"] / 1e6

    print(f"\n{'='*70}")
    print(f"  {path.name}  ({file_mb:.1f} MB)")
    print(f"{'='*70}")
    print(f"  Non-smask images found  : {result['n_images']}")
    print(f"  Images with placements  : {result['n_placed']}")
    print(f"  Downsample candidates   : {result['n_candidates']}  "
          f"(effective DPI > {target_dpi:.0f} × 1.1)")
    print(f"  Est. image data saving  : {saving_mb:.1f} MB  "
          f"(from image stream bytes only — actual PDF saving will be less)")

    if verbose and result["n_candidates"] > 0:
        print()
        print(f"  {'ObjNum':>7}  {'Pixels':>13}  {'Display(pts)':>14}  "
              f"{'EffDPI':>7}  {'Target px':>13}  {'Ratio':>6}  {'Save KB':>8}")
        print(f"  {'-'*7}  {'-'*13}  {'-'*14}  {'-'*7}  {'-'*13}  {'-'*6}  {'-'*8}")
        for r in sorted(result["rows"], key=lambda x: -(x["estimated_saving_bytes"])):
            if not r["can_downsample"]:
                continue
            print(
                f"  {r['objnum']:>7d}  "
                f"{r['px_w']:>5d}×{r['px_h']:<5d}  "
                f"{r['max_display_w_pts']:>6.0f}×{r['max_display_h_pts']:<6.0f}  "
                f"{r['effective_dpi']:>7.0f}  "
                f"{r['target_px_w']:>5d}×{r['target_px_h']:<5d}  "
                f"{r['downsample_ratio']:>6.2f}  "
                f"{r['estimated_saving_bytes']//1024:>8d}"
            )


def main():
    parser = argparse.ArgumentParser(
        description="Estimate PDF image downsampling savings"
    )
    parser.add_argument("inputs", nargs="+", help="PDF files to analyse")
    parser.add_argument(
        "--target-dpi", type=float, default=150,
        help="Target PPI after downsampling (default: 150)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print per-image breakdown for downsample candidates"
    )
    args = parser.parse_args()

    total_saving = 0
    total_file_size = 0

    for path_str in args.inputs:
        path = Path(path_str)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            continue
        result = analyze_pdf(path, args.target_dpi)
        print_report(result, args.target_dpi, args.verbose)
        total_saving += result["estimated_saving_bytes"]
        total_file_size += result["file_size_mb"] * 1e6

    if len(args.inputs) > 1:
        print(f"\n{'='*70}")
        print(f"  TOTAL across all files")
        print(f"  Est. image stream saving: {total_saving/1e6:.1f} MB  "
              f"at target DPI={args.target_dpi:.0f}")
        print(f"  Note: actual PDF file saving ≈ 60–80% of image stream saving")
        print(f"        (PDF structure overhead, already-compressed images, etc.)")


if __name__ == "__main__":
    main()
