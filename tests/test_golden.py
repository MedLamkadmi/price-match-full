#!/usr/bin/env python3
"""Golden tests — each pins a gate from Ellie's 28-step logic. Run: python3 tests/test_golden.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from matcher import parse_item, classify, parse_price, comparable_unit_price, EXACT, STRONG, RELATED, NO_MATCH

passed = failed = 0
def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print("FAIL %-58s got=%s want=%s" % (name, got, want))

def grade(master_raw, cand_raw):
    m = parse_item(master_raw, is_master=True)[0]
    # merge OR branches' brands (as main() does)
    ms = parse_item(master_raw, is_master=True)
    m.brands_allowed = sorted({b for x in ms for b in x.brands_allowed})
    c = parse_item(cand_raw)[0]
    g, reasons = classify(m, c)
    return g

# --- brand gate ---
check("or-brand: Five Roses satisfies 'Robin Hood or Five Roses'",
      grade("Robin Hood or Five Roses Flour 10kg", "Five Roses Flour 10 kg"), EXACT)
check("or-brand: Robin Hood satisfies",
      grade("Robin Hood or Five Roses Flour 10kg", "Robin Hood Flour 10 kg"), EXACT)
check("brand fail: Selection -> RELATED not EXACT",
      grade("Robin Hood or Five Roses Flour 10kg", "Selection Flour 10 kg"), RELATED)
# --- variant gate: brand alone never enough ---
check("variant: Whole Wheat vs (no variant specified) -> STRONG",
      grade("Robin Hood Flour 10 kg", "Robin Hood Whole Wheat Flour 10 kg"), STRONG)
check("variant: Cake Mix is a different product -> NO_MATCH",
      grade("Robin Hood or Five Roses All Purpose Flour 2.5 kg", "Robin Hood Cake Mix 2.5 kg"), NO_MATCH)
# --- unit normalization ---
check("units: 6 oz == 170 g",
      grade("Blueberries 6 oz", "Blueberries 170 g"), EXACT)
check("units: ea == each (cauliflower)",
      grade("Cauliflower 1 ea", "Cauliflower each"), EXACT)
check("units: 2.5 kg == 2500 g",
      grade("Robin Hood Flour 2.5 kg", "Robin Hood Flour 2500 g"), EXACT)
# --- quantity gatekeeper: same product, two sizes must NOT cross-match ---
check("gatekeeper: Blueberries Pint vs 6 oz -> not EXACT",
      grade("Blueberries Pint", "Blueberries 6 oz") != EXACT, True)
# --- ranges ---
check("range: 340g inside 267-383g -> EXACT",
      grade("Taylor Farms Chopped Salad Kits 267-383 g", "Taylor Farms Chopped Salad Kit 340 g"), EXACT)
check("range: wrong size -> STRONG collision (not silent)",
      grade("Robin Hood Flour 2.5 kg", "Robin Hood Flour 5 kg"), STRONG)
# --- rolls: never collapse 12 into 24 ---
check("rolls: 12=24 vs '12 Mega Rolls = 24 Regular Rolls' -> EXACT",
      grade("Cashmere Bathroom Tissue 12=24", "Cashmere 12 Mega Rolls = 24 Regular Rolls"), EXACT)
check("rolls: physical 12 vs '12 Mega Rolls' -> STRONG (product unverified, brand+size decide)",
      grade("Cashmere Bathroom Tissue 12=24", "Cashmere 12 Mega Rolls"), STRONG)
# --- multipack format matters ---
check("multipack: 6x710ml identical -> EXACT",
      grade("Pepsi 6x710 ml", "Pepsi 6x710 ml"), EXACT)
# --- or-variants ---
check("or-variant: 'Robin Hood or Five Roses' either brand",
      grade("Maple Leaf or Schneiders Bacon 375 g", "Schneiders Bacon 375 g"), EXACT)
# --- prices ---
p = parse_price("2/$7")
check("multibuy: effective each 3.50", p.effective_each, 3.50)
check("multibuy: eligibility class 1 (no penalty)", p.eligibility, 1)
check("multibuy: comparable unit price", comparable_unit_price(p), 3.50)
p2 = parse_price("2/$7, less than 2 pay $4.99")
check("multibuy penalty: eligibility class 2", p2.eligibility, 2)
pm = parse_price("Member $3.99 / Non-member $4.99")
check("member: keeps both paths", (pm.member_price, pm.nonmember_price), (3.99, 4.99))
check("member: unconditional $4.99 used for primary (Step 9)", comparable_unit_price(pm), 4.99)
plb = parse_price("$5.99/lb")
check("per-lb converts to per-kg", round(comparable_unit_price(plb), 2), 13.21)

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
