#!/usr/bin/env python3
"""Extractor unit tests (source-agnostic logic). Run: python3 tests/test_extractor.py"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from extractor.raddar import _week_bounds, _launches_in_week, _slugify
from extractor.models import FlyerMeta, FlyerItem
from extractor.images import cached_image_path

passed = failed = 0
def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("FAIL %-50s got=%s want=%s" % (name, got, want))

# week bounds: 2026-W41 -> Mon 2026-10-05 .. Sun 2026-10-11
mon, sun = _week_bounds("2026-W41")
check("week monday", str(mon), "2026-10-05")
check("week sunday", str(sun), "2026-10-11")

# only NEW flyers launching that week (not still-active old ones)
check("launches Tue of week -> include",
      _launches_in_week("2026-10-06", mon, sun), True)
check("launched previous week -> exclude",
      _launches_in_week("2026-09-29", mon, sun), False)
check("launches next week -> exclude",
      _launches_in_week("2026-10-13", mon, sun), False)
check("unknown date -> include (never silently drop)",
      _launches_in_week("", mon, sun), True)

check("slugify", _slugify("No Frills"), "no-frills")

# model serialization contract with the matcher
f = FlyerMeta(retailer="Walmart", flyer_id="WM-41",
              valid_from="2026-10-06", valid_to="2026-10-12",
              items=[FlyerItem(title="Cauliflower 1 ea", price="$1.97", page=3)])
d = f.to_dict()
check("to_dict retailer", d["retailer"], "Walmart")
check("to_dict item title", d["items"][0]["title"], "Cauliflower 1 ea")
check("to_dict item price", d["items"][0]["price"], "$1.97")

p = cached_image_path("https://x.com/a.png?x=1", "/tmp/imgtest")
check("image cache ext", p.endswith(".png"), True)
check("no url -> no path", cached_image_path("", "/tmp/imgtest"), None)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
