#!/usr/bin/env python3
"""End-to-end weekly pipeline: extract flyers -> match -> Excel.

Usage:
  python pipeline.py --week 2026-W41
  python pipeline.py --week 2026-W41 --skip-extract   # reuse data/flyers_2026-W41.json
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description="Weekly price-match pipeline")
    ap.add_argument("--week", required=True)
    ap.add_argument("--skip-extract", action="store_true")
    ap.add_argument("--retailers", nargs="*", default=None)
    args = ap.parse_args()

    flyers_path = os.path.join(BASE, "data", "flyers_%s.json" % args.week)
    if not args.skip_extract:
        cmd = [sys.executable, "-m", "extractor.extract", "--week", args.week,
               "--download-images"]
        if args.retailers:
            cmd += ["--retailers"] + args.retailers
        print("+", " ".join(cmd))
        subprocess.run(cmd, cwd=BASE, check=True)
    elif not os.path.exists(flyers_path):
        sys.exit("no flyer data at %s (run without --skip-extract)" % flyers_path)

    # Feed the extracted flyers into the matcher via its expected input file.
    with open(flyers_path, encoding="utf-8") as f:
        data = json.load(f)
    with open(os.path.join(BASE, "data", "flyers_sample.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    print("+ python matcher.py")
    subprocess.run([sys.executable, "matcher.py"], cwd=BASE, check=True)

    demo = os.path.join(BASE, "output", "price_match_demo.xlsx")
    final = os.path.join(BASE, "output", "price_match_%s.xlsx" % args.week)
    if os.path.exists(demo):
        os.replace(demo, final)
    print("done ->", final)


if __name__ == "__main__":
    main()
