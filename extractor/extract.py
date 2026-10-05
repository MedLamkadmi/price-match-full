#!/usr/bin/env python3
"""Pull this week's flyers -> flyers_YYYY-Www.json (the matcher's input).

Usage:
  python -m extractor.extract --week 2026-W41
  python -m extractor.extract --week 2026-W41 --retailers Walmart "No Frills" Superstore
  python -m extractor.extract --week 2026-W41 --source raddar --download-images
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from extractor.images import fetch_image
from extractor.http import make_session

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_retailers():
    with open(os.path.join(BASE, "config", "retailers.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["retailers"], cfg.get("extractor", {})


def get_source(name, session):
    if name == "raddar":
        from extractor.raddar import RaddarSource
        return RaddarSource(session)
    raise ValueError("unknown source: %s" % name)


def main():
    ap = argparse.ArgumentParser(description="Extract weekly flyers")
    ap.add_argument("--week", required=True, help="e.g. 2026-W41")
    ap.add_argument("--retailers", nargs="*", default=None)
    ap.add_argument("--source", default="raddar")
    ap.add_argument("--download-images", action="store_true")
    args = ap.parse_args()

    retailers_cfg, ext_cfg = load_retailers()
    session = make_session(delay=ext_cfg.get("request_delay_seconds", 1.5),
                           timeout=ext_cfg.get("timeout_seconds", 30),
                           max_retries=ext_cfg.get("max_retries", 3))
    source = get_source(args.source, session)
    flyers = source.extract_week(args.week, args.retailers)

    cache_dir = os.path.join(BASE, ext_cfg.get("image_cache_dir", "data/image_cache"))
    total_items = 0
    for f in flyers:
        total_items += len(f.items)
        if args.download_images:
            for it in f.items:
                if it.image_url:
                    it.image_path = fetch_image(session, it.image_url, cache_dir) or ""
        print("%-12s %-28s %4d items  (%s to %s)" % (
            f.retailer, f.flyer_id, len(f.items), f.valid_from, f.valid_to))

    out = os.path.join(BASE, "data", "flyers_%s.json" % args.week)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"week": args.week, "flyers": [f.to_dict() for f in flyers]},
                  fh, ensure_ascii=False, indent=1)
    print("flyers: %d | items: %d -> %s" % (len(flyers), total_items, out))


if __name__ == "__main__":
    main()
