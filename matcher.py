#!/usr/bin/env python3
"""
Price-match engine — parse-first gates matcher implementing Ellie's 28-step logic.

Mantra (Step 28): "Do not compare strings. Compare structured product attributes."
Unsure rule (Step 24): uncertainty DOWNGrades, never upgrades.
    "I would rather miss an EXACT than falsely claim one."

Pipeline: parse master + candidates -> gates (Step 23 tree) -> grades
    -> Stage 1 (lowest comparable competitor) -> Stage 2 (Superstore compare)
    -> Excel (Ellie-facing + Review queue + Audit).
RapidFuzz is a fallback INSIDE the product gate (typos/word order), never the decider.
"""

import re
import json
import csv
import os
import sys
import yaml
import hashlib
from dataclasses import dataclass, field
from rapidfuzz import fuzz

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- config ---

def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

SETTINGS = load_yaml(os.path.join(BASE, "config", "settings.yaml"))
BRANDS = [b.lower() for b in load_yaml(os.path.join(BASE, "config", "brands.yaml"))["brands"]]
SYNONYMS = load_yaml(os.path.join(BASE, "config", "synonyms.yaml"))["synonyms"]
UNITS = load_yaml(os.path.join(BASE, "config", "units.yaml"))
W2G = UNITS["weight_to_g"]
V2ML = UNITS["volume_to_ml"]

SIM_THRESHOLD = SETTINGS.get("product_similarity_threshold", 85)
SIZE_TOL = SETTINGS.get("size_tolerance_pct", 2.0) / 100.0
RANGE_CLOSE = SETTINGS.get("range_close_pct", 10.0) / 100.0

# ------------------------------------------------------------- text utils ---

def normalize_text(s: str) -> str:
    s = s.lower()
    for k, v in SYNONYMS.items():
        s = re.sub(r"\b" + re.escape(k) + r"\b", v, s)
    s = re.sub(r"[^a-z0-9\s./=x×-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

STOPWORDS = {"regular", "mega", "jumbo", "double", "triple",
             # flyer/packaging jargon, not product identity
             "club", "size", "selected", "varieties", "variety", "assorted"}

def tokens(s: str):
    out = []
    for w in normalize_text(s).split():
        if w in STOPWORDS:
            continue
        # light plural stemming ("kits"->"kit"); applied consistently both sides
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out

# --------------------------------------------------------------- dataclasses ---

@dataclass
class SizeInfo:
    kind: str = "none"          # weight | volume | count | each | rolls | multipack | none
    value: float = None         # canonical: grams | mL | count
    unit_raw: str = None
    range_min: float = None     # canonical
    range_max: float = None
    selected_sizes: bool = False
    pack_count: int = None      # multipack (Step 11)
    pack_unit_size: float = None
    pack_total: float = None
    pack_format: str = None     # e.g. 'cans', 'tub', 'bottles'
    roll_physical: int = None   # Step 10
    roll_equivalent: int = None

@dataclass
class PriceInfo:
    advertised: str             # raw, e.g. "2/$7"
    amount: float               # headline numeric
    per_basis: str = None       # 'lb' | 'kg' | 'each' | None
    effective_each: float = None
    required_qty: int = None
    eligibility: int = 1        # 1 unconditional, 2 qty-conditional, 3 member/app, 4 multi
    conditions: list = field(default_factory=list)
    member_price: float = None
    nonmember_price: float = None
    display: str = ""           # "2/$7 ($3.50 ea.; must buy 2)"

@dataclass
class ParsedItem:
    raw: str
    brands_allowed: list = field(default_factory=list)  # master: set; candidate: [brand] or []
    product: str = ""           # normalized product descriptor tokens
    variant: str = ""           # extra specifying tokens, e.g. "whole wheat"
    size: SizeInfo = field(default_factory=SizeInfo)
    is_master: bool = False

# ------------------------------------------------------------ size parsing ---

SIZE_RE = re.compile(
    r"(?P<lo>\d+(?:\.\d+)?)\s*[-–/]\s*(?P<hi>\d+(?:\.\d+)?)\s*(?P<u>g|kg|ml|l|oz|lb)\b", re.I)
MULTI_RE = re.compile(
    r"(?P<n>\d+)\s*[x×]\s*(?P<v>\d+(?:\.\d+)?)\s*(?P<u>g|kg|ml|l|oz)\b", re.I)
PACK_RE = re.compile(r"(?P<n>\d+)\s*(?P<u>pk|pack|pks|ct|count)\b", re.I)
EACH_RE = re.compile(r"(?:(?P<n>\d+)\s*)?(?P<u>ea|each)\b", re.I)  # bare "each" = 1
W_RE = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*(?P<u>kg|g|oz|lb)\b", re.I)
V_RE = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*(?P<u>ml|l)\b", re.I)
ROLL_RE = re.compile(r"(?P<a>\d+)\s*=\s*(?P<b>\d+)", re.I)
ROLL2_RE = re.compile(r"(?P<a>\d+)\s*(?:mega\s+)?rolls?\s*=\s*(?P<b>\d+)", re.I)
ROLLN_RE = re.compile(r"(?P<a>\d+)\s*(?:mega\s+)?rolls?\b", re.I)
PINT_RE = re.compile(r"\b(?P<n>\d*)\s*pints?\b", re.I)

def to_canonical(value: float, unit: str):
    unit = unit.lower()
    if unit in W2G:
        return ("weight", value * W2G[unit])
    if unit in V2ML:
        return ("volume", value * V2ML[unit])
    return ("none", None)

def parse_size(text: str) -> tuple[SizeInfo, str]:
    """Extract size info; return (SizeInfo, text_with_size_removed)."""
    info = SizeInfo()
    t = text
    # rolls: "12=24", "12 Mega Rolls = 24 Regular Rolls", "12 Mega Rolls"
    m = ROLL2_RE.search(t) or ROLL_RE.search(t)
    if m:
        info.kind = "rolls"
        info.roll_physical = int(m.group("a"))
        info.roll_equivalent = int(m.group("b"))
        t = (ROLL2_RE if "rolls" in m.group(0).lower() else ROLL_RE).sub(" ", t)
    else:
        m = ROLLN_RE.search(t)
        if m:
            info.kind = "rolls"
            info.roll_physical = int(m.group("a"))
            t = ROLLN_RE.sub(" ", t)
    # "selected sizes"
    if re.search(r"selected\s+sizes?", t, re.I):
        info.selected_sizes = True
        t = re.sub(r"selected\s+sizes?", " ", t, flags=re.I)
    # multipack: "6x710 ml"
    m = MULTI_RE.search(t)
    if m:
        kind, canon = to_canonical(float(m.group("v")), m.group("u"))
        info.kind = "multipack"
        info.pack_count = int(m.group("n"))
        info.pack_unit_size = canon
        info.pack_total = int(m.group("n")) * (canon or 0)
        # capture format word after, e.g. "cans", "tub", "bottles"
        fm = re.search(r"\b(cans?|tub|bottles?|packs?)\b", t[m.end():m.end() + 20], re.I)
        info.pack_format = fm.group(1).lower() if fm else None
        t = MULTI_RE.sub(" ", t)
    # range: "267-383 g"
    m = SIZE_RE.search(t)
    if m and info.kind == "none":
        kind, lo = to_canonical(float(m.group("lo")), m.group("u"))
        _, hi = to_canonical(float(m.group("hi")), m.group("u"))
        info.kind = kind
        info.range_min, info.range_max = lo, hi
        t = SIZE_RE.sub(" ", t)
    # each: "1 ea" or bare "each" (= 1)
    m = EACH_RE.search(t)
    if m and info.kind in ("none",):
        info.kind = "each"
        info.value = float(m.group("n")) if m.group("n") else 1.0
        t = EACH_RE.sub(" ", t, count=1)
    # pack count: "30 pk", "12 ct"
    m = PACK_RE.search(t)
    if m and info.kind == "none":
        info.kind = "count"
        info.value = float(m.group("n"))
        t = PACK_RE.sub(" ", t)
    # pint
    m = PINT_RE.search(t)
    if m and info.kind == "none":
        info.kind = "volume_pint"
        info.value = 473.0 if not m.group("n") else float(m.group("n")) * 473.0
        t = PINT_RE.sub(" ", t)
    # plain weight / volume (only if nothing found yet)
    if info.kind == "none":
        m = W_RE.search(t)
        if m:
            kind, canon = to_canonical(float(m.group("v")), m.group("u"))
            info.kind, info.value = kind, canon
            info.unit_raw = m.group("u").lower()
            t = W_RE.sub(" ", t)
        else:
            m = V_RE.search(t)
            if m:
                kind, canon = to_canonical(float(m.group("v")), m.group("u"))
                info.kind, info.value = kind, canon
                info.unit_raw = m.group("u").lower()
                t = V_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return info, t

# ------------------------------------------------------------ price parsing ---
# Eligibility classes (Steps 8-20, price series):
#   1 unconditional | 2 quantity-conditional | 3 membership/app | 4 multiple

PRICE_RE = re.compile(r"\$\s*(?P<v>\d+(?:\.\d+)?)\s*(?:/\s*(?P<b>lb|kg|each|ea))?", re.I)
MULTIBUY_RE = re.compile(r"(?P<n>\d+)\s*/\s*\$\s*(?P<v>\d+(?:\.\d+)?)", re.I)
MULTIBUY2_RE = re.compile(r"(?P<n>\d+)\s+for\s+\$\s*(?P<v>\d+(?:\.\d+)?)", re.I)
MEMBER_RE = re.compile(
    r"members?\s*\$?\s*(?P<m>\d+(?:\.\d+)?)\s*/\s*non[\s-]*members?\s*\$?\s*(?P<n>\d+(?:\.\d+)?)", re.I)
APP_RE = re.compile(r"app\s+(?:price\s+)?\$\s*(?P<a>\d+(?:\.\d+)?)", re.I)
BOGO_RE = re.compile(r"buy\s*(?P<n>\d+)\s*,?\s*get\s*(?P<f>\d+)\s*free", re.I)

def parse_price(raw: str) -> PriceInfo | None:
    if not raw:
        return None
    t = raw.strip()
    info = PriceInfo(advertised=t, amount=0.0, display=t)
    # member pricing: keep BOTH paths (Step 8)
    m = MEMBER_RE.search(t)
    if m:
        info.member_price = float(m.group("m"))
        info.nonmember_price = float(m.group("n"))
        info.amount = info.nonmember_price  # unconditional path is primary (Step 9)
        info.eligibility = 3
        info.conditions = ["member price; non-member pays $%.2f" % info.nonmember_price]
        info.display = "Member $%.2f / Non-member $%.2f" % (info.member_price, info.nonmember_price)
        return info
    # app-only
    m = APP_RE.search(t)
    if m and re.search(r"regular\s*\$\s*(\d+(?:\.\d+)?)", t, re.I):
        reg = float(re.search(r"regular\s*\$\s*(\d+(?:\.\d+)?)", t, re.I).group(1))
        info.member_price = float(m.group("a"))
        info.nonmember_price = reg
        info.amount = reg
        info.eligibility = 3
        info.conditions = ["app activation required"]
        info.display = "App $%.2f / Regular $%.2f (app required)" % (info.member_price, reg)
        return info
    # multi-buy: "2/$7" / "2 for $7"
    m = MULTIBUY_RE.search(t) or MULTIBUY2_RE.search(t)
    if m:
        n, v = int(m.group("n")), float(m.group("v"))
        info.required_qty = n
        info.effective_each = round(v / n, 4)
        info.amount = info.effective_each
        # quantity penalty? "otherwise $4.99" / "less than 2 pay $4.99"
        penalty = re.search(r"(?:otherwise|less than \d+ pay)\s*\$?\s*(\d+(?:\.\d+)?)", t, re.I)
        member_only = bool(re.search(r"members?\s*(?:only|:)", t, re.I))
        if member_only:
            info.eligibility = 4
            info.conditions = ["member price", "buy %d" % n]
            info.display = "%d/$%g ($%.2f ea.; member price; buy %d)" % (n, v, info.effective_each, n)
        elif penalty:
            info.eligibility = 2
            info.conditions = ["must buy %d (otherwise $%s each)" % (n, penalty.group(1))]
            info.display = "%d/$%g ($%.2f ea.; must buy %d)" % (n, v, info.effective_each, n)
        else:
            # Case A (Step 5): singles allowed at effective price -> effectively unconditional
            info.eligibility = 1
            info.conditions = ["buy %d" % n]
            info.display = "%d/$%g ($%.2f ea.; buy %d)" % (n, v, info.effective_each, n)
        return info
    # buy X get Y free without base price -> cannot derive unit price (Step 7)
    if BOGO_RE.search(t):
        info.eligibility = 2
        info.conditions = ["promotion mechanics not directly comparable"]
        info.display = t + " (cannot derive unit price)"
        return info
    # plain price, maybe per-unit basis: "$5.99/lb"
    m = PRICE_RE.search(t)
    if m:
        info.amount = float(m.group("v"))
        b = m.group("b")
        info.per_basis = b.lower() if b else None
        if info.per_basis == "ea":
            info.per_basis = "each"
        info.effective_each = info.amount if info.per_basis in (None, "each") else None
        return info
    return None

def comparable_unit_price(p: PriceInfo | None) -> float | None:
    """Price per tracked unit on a common basis (Step 19). None if not derivable."""
    if p is None:
        return None
    pol = SETTINGS["price_policy"]
    # Member/app pricing: the NON-MEMBER path is the unconditional comparable (Step 9).
    # "Prevents a loyalty-only price from silently defeating an ordinary public price."
    if p.eligibility == 3 and p.nonmember_price is not None:
        return p.nonmember_price
    if p.eligibility >= 3 and not pol["allow_member_price_as_primary"]:
        return None  # member/app prices never silent winners
    if p.eligibility == 2 and not pol["allow_multibuy_as_primary"]:
        return None
    if p.effective_each is not None:
        return p.effective_each
    if p.per_basis == "lb" and SETTINGS["assumptions"]["per_weight_basis"] == "kg":
        return p.amount * 2.20462  # $/lb -> $/kg
    if p.per_basis in (None, "each", "kg"):
        return p.amount
    return None

# ------------------------------------------------------------- item parsing ---

def extract_brands(text: str) -> tuple[list, str]:
    """Find known brand tokens; return (brands_found, text_without_brands)."""
    found = []
    t = " " + text.lower() + " "
    for b in sorted(BRANDS, key=len, reverse=True):
        if " " + b + " " in t or t.startswith(b + " ") or t.endswith(" " + b):
            found.append(b)
            t = re.sub(r"\b" + re.escape(b) + r"\b", " ", t)
    # also catch "Brand's" possessives missed above
    t = re.sub(r"\s+", " ", t).strip()
    # title-case for display
    found = [b.title() if b != "no name" else "no name" for b in found]
    return found, t

def split_alternatives(text: str) -> list[str]:
    """Split on ' or ' and '/' into alternatives (Steps 12-15)."""
    parts = re.split(r"\s+or\s+|(?<=.)/(?=.)", text, flags=re.I)
    return [p.strip(" ,") for p in parts if p.strip(" ,")]

def parse_item(raw: str, is_master: bool = False) -> list[ParsedItem]:
    """
    Parse a title into structured attributes (Step 28).
    Returns a list: OR-branches expand to multiple ParsedItems sharing price/page.
    """
    size, rest = parse_size(raw)
    alts = split_alternatives(rest)
    # shared product descriptor = tokens common to all alternatives (the suffix)
    out = []
    for alt in alts:
        brands, no_brand = extract_brands(alt)
        toks = tokens(no_brand)
        # crude variant detection: known variant keywords stay as variant
        variant_kw = {"all purpose", "whole wheat", "cake mix", "wavy", "oven baked",
                      "sandwich", "texas", "bread"}
        # variant = tokens after product core: heuristic — keep all, compare as sets later
        item = ParsedItem(
            raw=raw,
            brands_allowed=brands if brands else [],
            product=" ".join(toks),
            variant="",
            size=size,
            is_master=is_master,
        )
        out.append(item)
    # If first alternative had brand-only ("Robin Hood") and product lives in later
    # alternatives, propagate the shared product descriptor.
    products = [p.product for p in out if p.product]
    if products:
        shared = max(set(products), key=products.count)
        for p in out:
            if not p.product:
                p.product = shared
    return out

def product_similarity(a: str, b: str) -> float:
    return fuzz.token_set_ratio(a, b)

# ------------------------------------------------------------------- gates ---
# Step 23 decision tree:
#   1. relevant? no -> reject (NO_MATCH)
#   2. brand satisfies? no -> RELATED at best
#   3. same product family/type? no -> RELATED or reject
#   4. same variant/format? no -> STRONG or RELATED, never EXACT
#   5. normalize units (done in parsing)
#   6. same size/pack? yes -> EXACT candidate

# Variant keywords: omitting one of these = unverifiable variant -> STRONG.
# Omitting a generic product word (e.g. "bathroom" in "bathroom tissue") is fine.
VARIANT_KW = {"all", "purpose", "whole", "wheat", "cake", "mix", "wavy", "oven",
              "baked", "sandwich", "texas", "white", "red", "green", "yellow",
              "large", "small", "medium", "baby", "thin", "thick", "boneless"}

EXACT, STRONG, RELATED, NO_MATCH = "EXACT", "STRONG", "RELATED", "NO_MATCH"

def sizes_equal(a: SizeInfo, b: SizeInfo) -> tuple[bool, str]:
    """Compare two SizeInfos. Returns (equal, note)."""
    # rolls: compare physical first (Step 10) — never collapse
    if a.kind == "rolls" or b.kind == "rolls":
        ap, bp = a.roll_physical, b.roll_physical
        ae, be = a.roll_equivalent, b.roll_equivalent
        if ap and bp and ap == bp:
            return True, "physical roll count matches (%d)" % ap
        if ae and be and ae == be:
            return False, "equivalent count matches (%d) but physical differs" % ae
        if ap and be and ap == be:
            return False, "physical %d matches equivalent %d" % (ap, be)
        return False, "roll counts differ"
    # multipack: same total is NOT exact if format differs (Step 11)
    if a.kind == "multipack" or b.kind == "multipack":
        if a.kind == "multipack" and b.kind == "multipack":
            if (a.pack_count == b.pack_count and
                    abs((a.pack_unit_size or 0) - (b.pack_unit_size or 0)) < 1e-6):
                return True, "multipack identical"
            return False, "multipack format differs"
        # one side multipack, other single with same total -> STRONG territory
        ta = a.pack_total if a.kind == "multipack" else a.value
        tb = b.pack_total if b.kind == "multipack" else b.value
        if ta and tb and abs(ta - tb) / max(ta, tb) <= SIZE_TOL:
            return False, "same total, different format"
        return False, "multipack vs single, totals differ"
    # selected sizes: weak (Step 9)
    if a.selected_sizes or b.selected_sizes:
        return False, "'selected sizes' — size not verifiable"
    # dimension mismatch (e.g. volume pint vs weight grams): cannot verify -> not exact
    dims = {"weight", "volume", "volume_pint", "count", "each"}
    if a.kind in dims and b.kind in dims and a.kind != b.kind:
        # pint is volume; grams weight — no stated conversion
        return False, "size dimension differs (%s vs %s), conversion not verified" % (a.kind, b.kind)
    # range handling (Steps 7-8)
    av = a.value if a.kind != "none" else None
    bv = b.value if b.kind != "none" else None
    if a.range_min is not None and bv is not None:  # master range, candidate single
        if a.range_min <= bv <= a.range_max:
            return True, "%.0f inside range %.0f-%.0f" % (bv, a.range_min, a.range_max)
        return False, "%.0f outside range %.0f-%.0f" % (bv, a.range_min, a.range_max)
    if b.range_min is not None and av is not None:  # candidate range covers master
        if b.range_min <= av <= b.range_max:
            return True, "master %.0f inside advertised range %.0f-%.0f" % (av, b.range_min, b.range_max)
        return False, "master %.0f outside advertised range" % av
    if a.range_min is not None and b.range_min is not None:
        # identical ranges -> EXACT. Containment either way -> EXACT (Step 7:
        # "flyer range contains the tracked size"; tracked band containing the
        # flyer size equally satisfies). Partial overlap -> STRONG (Step 8).
        if (abs(a.range_min - b.range_min) / max(a.range_min, 1) <= SIZE_TOL
                and abs(a.range_max - b.range_max) / max(a.range_max, 1) <= SIZE_TOL):
            return True, "identical ranges %.0f-%.0f" % (a.range_min, a.range_max)
        if a.range_min <= b.range_min and b.range_max <= a.range_max:
            return True, "flyer range %.0f-%.0f within tracked %.0f-%.0f" % (
                b.range_min, b.range_max, a.range_min, a.range_max)
        if b.range_min <= a.range_min and a.range_max <= b.range_max:
            return True, "tracked range %.0f-%.0f within flyer %.0f-%.0f" % (
                a.range_min, a.range_max, b.range_min, b.range_max)
        return False, "ranges partially overlap, not identical"
    # plain values
    if av is None or bv is None:
        return False, "size missing on one side"
    if abs(av - bv) / max(av, bv) <= SIZE_TOL:
        return True, "sizes equal (%.2f)" % av
    return False, "sizes differ (%.2f vs %.2f)" % (av, bv)


def gate_size(master: ParsedItem, cand: ParsedItem) -> tuple[str, str]:
    """Size gate -> (grade, reason). Never upgrades beyond STRONG on doubt."""
    equal, note = sizes_equal(master.size, cand.size)
    if equal:
        return EXACT, "size: " + note
    # outside-but-close -> STRONG (Step 7 Ex.2 "commercially relevant")
    ms, cs = master.size, cand.size
    if (ms.value and cs.value and ms.kind == cs.kind
            and ms.kind in ("weight", "volume", "count", "each")):
        gap = abs(ms.value - cs.value) / max(ms.value, cs.value)
        if gap <= RANGE_CLOSE:
            return STRONG, "size: close but different (%s)" % note
        return STRONG, "size: collision, wrong pack (%s)" % note  # Step 16
    if "dimension differs" in note or "selected sizes" in note or "different format" in note \
            or "ranges partially overlap" in note or "outside range" in note:
        return STRONG, "size: " + note
    if "equivalent count" in note or "physical" in note and "matches equivalent" in note:
        return STRONG, "size: " + note
    return STRONG, "size: " + note  # default: preserve as collision (Step 16)


def classify(master: ParsedItem, cand: ParsedItem) -> tuple[str, list]:
    """
    Step 23 decision tree. Returns (grade, reasons).
    Uncertainty downgrades, never upgrades (Step 24).
    """
    reasons = []
    # Effective product descriptor: fall back to brand when size+brand consumed
    # everything (e.g. "Pepsi 6x710 ml" -> product "pepsi").
    mp = master.product or " ".join(master.brands_allowed)
    cp = cand.product or " ".join(cand.brands_allowed)
    product_unverified = False
    # Gate 1: relevant?
    sim = product_similarity(mp, cp)
    if sim < 60:
        if (not cand.product and master.brands_allowed and cand.brands_allowed
                and any(b.lower() in [x.lower() for x in master.brands_allowed]
                        for b in cand.brands_allowed)):
            # brand+size decide; product unverified -> can never be EXACT (Step 24)
            product_unverified = True
            reasons.append("product: descriptor missing on candidate; brand+size decide")
        else:
            return NO_MATCH, ["product not relevant (similarity %.0f)" % sim]
    # Gate 2: brand satisfies master brand rule?
    if master.brands_allowed:
        cb = [b.lower() for b in cand.brands_allowed]
        allowed = [b.lower() for b in master.brands_allowed]
        if not any(b in allowed for b in cb):
            # Step 17: same product, disallowed brand -> RELATED
            return RELATED, ["brand mismatch: candidate '%s' not in allowed %s"
                             % (", ".join(cand.brands_allowed) or "none", master.brands_allowed)]
        reasons.append("brand: '%s' allowed" % ", ".join(cand.brands_allowed))
    else:
        if cand.brands_allowed:
            reasons.append("brand: master has no brand constraint")
    # Gate 3: same product family/type?
    if not product_unverified:
        if sim < SIM_THRESHOLD:
            return RELATED, reasons + ["product type differs (similarity %.0f)" % sim]
        reasons.append("product: same family (similarity %.0f)" % sim)
    # Gate 4: same variant/format? brand alone never enough (Step 4).
    # Only contradictions and missing VARIANT keywords downgrade — a candidate that
    # simply says less about a generic product word still passes.
    if product_unverified:
        reasons.append("variant: skipped (product unverified)")
    else:
        mt, ct = set(mp.split()), set(cp.split())
        extra = ct - mt
        missing_variant = (mt - ct) & VARIANT_KW
        if extra:
            return STRONG, reasons + ["variant/format differs: candidate adds '%s'"
                                      % " ".join(sorted(extra))]
        if missing_variant:
            return STRONG, reasons + ["variant unverified: candidate omits '%s'"
                                      % " ".join(sorted(missing_variant))]
    # Gate 6: size/pack
    grade, reason = gate_size(master, cand)
    reasons.append(reason)
    if product_unverified and grade == EXACT:
        grade = STRONG
        reasons.append("capped at STRONG: product descriptor unverified (Step 24)")
    return grade, reasons

# ------------------------------------------------------- stages 1 & 2 ---

@dataclass
class Collision:
    retailer: str
    title: str
    page: object
    price: PriceInfo
    grade: str
    reasons: list
    candidate: ParsedItem = None
    image: str = ""   # local image path (real flyer image when extraction provided one)

def stage1(master: ParsedItem, flyer_items: list) -> dict:
    """
    Search every competitor flyer for qualifying collisions (Steps 1-24),
    then select the lowest verified directly comparable unconditional offer.
    Returns dict with primary, collisions, related.
    """
    collisions, related = [], []
    for it in flyer_items:
        for cand in parse_item(it["title"]):
            grade, reasons = classify(master, cand)
            price = parse_price(it.get("price", ""))
            if grade == NO_MATCH:
                continue
            c = Collision(retailer=it["retailer"], title=it["title"], page=it.get("page"),
                          price=price, grade=grade, reasons=reasons, candidate=cand,
                          image=it.get("image", ""))
            if grade == RELATED:
                related.append(c)
            else:
                collisions.append(c)
    # Primary winner: lowest unconditional comparable EXACT (Steps 16-17, price series).
    # Multi-buy (class 2) may compete, marked; member/app (3/4) never silent winners.
    def primary_key(c):
        p = comparable_unit_price(c.price)
        return (p is None, p if p is not None else 0)
    exacts = sorted([c for c in collisions if c.grade == EXACT], key=primary_key)
    primary = None
    for c in exacts:
        if comparable_unit_price(c.price) is not None:
            primary = c
            break
    return {"primary": primary, "collisions": collisions, "related": related}


def stage2(master: ParsedItem, ss_items: list, s1primary: Collision | None) -> dict:
    """
    Independently search the full Superstore flyer, select the lowest directly
    comparable Superstore collision, compare with the Stage 1 winner.
    Central rule: same qualifying product + same package/price basis +
                 compatible promotion conditions -> compare normalized prices;
                 otherwise NOT DIRECTLY COMPARABLE.
    """
    collisions = []
    for it in ss_items:
        for cand in parse_item(it["title"]):
            grade, reasons = classify(master, cand)
            if grade in (EXACT, STRONG):
                collisions.append(Collision(
                    retailer="Superstore", title=it["title"], page=it.get("page"),
                    price=parse_price(it.get("price", "")), grade=grade,
                    reasons=reasons, candidate=cand, image=it.get("image", "")))
    def key(c):
        p = comparable_unit_price(c.price)
        return (p is None, p if p is not None else 0)
    ss_best = None
    for c in sorted([c for c in collisions if c.grade == EXACT], key=key):
        if comparable_unit_price(c.price) is not None:
            ss_best = c
            break
    if s1primary is None and ss_best is not None:
        return {"verdict": "NO_COMPETITOR_MATCH",
                "note": "Superstore match found — no qualifying Stage 1 competitor result available for comparison.",
                "ss": ss_best, "s1": None}
    if s1primary is None or ss_best is None:
        return {"verdict": "NO_MATCH", "note": "insufficient data", "ss": ss_best, "s1": s1primary}
    # same price class? (Step 18: compare like with like; Step 19: strict)
    c1, c2 = s1primary.price.eligibility, ss_best.price.eligibility
    p1, p2 = comparable_unit_price(s1primary.price), comparable_unit_price(ss_best.price)
    if c1 != c2:
        return {"verdict": "NOT_DIRECTLY_COMPARABLE",
                "note": "eligibility classes differ (competitor class %d vs Superstore class %d)" % (c1, c2),
                "ss": ss_best, "s1": s1primary}
    if p1 is None or p2 is None:
        return {"verdict": "NOT_DIRECTLY_COMPARABLE", "note": "price not derivable",
                "ss": ss_best, "s1": s1primary}
    diff = round(p2 - p1, 4)
    if abs(diff) < 0.005:
        verdict = "EQUAL"
    elif diff < 0:
        verdict = "CHEAPER"
    else:
        verdict = "MORE_EXPENSIVE"
    return {"verdict": verdict, "diff_per_unit": diff, "ss": ss_best, "s1": s1primary,
            "note": ""}

# ------------------------------------------------------------- thumbnails ---

def thumb_for(name: str, size=(90, 90)):
    """Deterministic placeholder thumbnail (real flyer images plug in later)."""
    from PIL import Image, ImageDraw, ImageFont
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    color = (100 + h % 100, 120 + (h >> 8) % 80, 120 + (h >> 16) % 80)
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    try:
        fnt = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
    except Exception:
        fnt = ImageFont.load_default()
    words, lines, cur = name.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fnt) <= size[0] - 10:
            cur = t
        else:
            lines.append(cur); cur = w
    lines.append(cur)
    y = (size[1] - len(lines) * 16) // 2
    for ln in lines[:4]:
        bb = d.textbbox((0, 0), ln, font=fnt)
        d.text(((size[0] - (bb[2] - bb[0])) // 2, y), ln, font=fnt, fill=(255, 255, 255))
        y += 16
    return im

# ------------------------------------------------------------- excel output ---

def write_excel(results: list, out_path: str):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.drawing.image import Image as XLImage
    import tempfile

    wb = Workbook()
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    GRADE_FILL = {
        "EXACT": ("C6EFCE", "006100"),
        "STRONG": ("FFEB9C", "9C6500"),
        "RELATED": ("FCE4D6", "7B3F00"),
        "NO_MATCH": ("F2F2F2", "808080"),
    }
    VERDICT_FILL = {
        "MORE_EXPENSIVE": ("FFC7CE", "9C0006"),  # FLAG: lower our price
        "CHEAPER": ("C6EFCE", "006100"),          # we win
        "EQUAL": ("DDEBF7", "1F4E79"),
        "NOT_DIRECTLY_COMPARABLE": ("FCE4D6", "7B3F00"),
        "NO_COMPETITOR_MATCH": ("E2EFDA", "375623"),
        "NO_MATCH": ("F2F2F2", "808080"),
    }

    # ---- Sheet 1: Ellie-facing ----
    ws = wb.active
    ws.title = "Price Match"
    headers = ["Tracked Item", "Lowest Price", "Retailer", "Page", "Match Status",
               "Competitor Title", "Image", "SS Price", "SS Title", "Image", "SS Page",
               "SS vs Comp.", "Difference", "Other Collisions"]
    widths = [28, 16, 13, 7, 13, 30, 12, 16, 30, 12, 8, 18, 12, 50]
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    c = ws.cell(1, 1, "PRICE MATCH — Stage 1 + Stage 2 (Ellie's gates)")
    c.font = Font(bold=True, size=14, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="DA291C")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30
    for i, (h, w) in enumerate(zip(headers, widths), start=1):
        cell = ws.cell(2, i, h)
        cell.font = Font(bold=True, size=10, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="404040")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
        ws.column_dimensions[cell.column_letter].width = w
    ws.row_dimensions[2].height = 36

    tmpdir = tempfile.mkdtemp(prefix="pm_thumbs_")
    r = 3
    for res in results:
        r += 1
        ws.row_dimensions[r].height = 118
        s1, s2 = res["stage1"], res["stage2"]
        p = s1["primary"]
        verdict = s2["verdict"]
        fill_hex, font_hex = VERDICT_FILL.get(verdict, ("FFFFFF", "000000"))
        if p:
            other = "; ".join(
                "%s — %s, p.%s, %s" % (c.retailer,
                                       c.price.display if c.price else "no price",
                                       c.page, c.grade)
                for c in s1["collisions"] if c is not p) or "—"
            verdict_disp = verdict
            if s2.get("diff_per_unit") is not None:
                verdict_disp += " ($%.2f/unit)" % s2["diff_per_unit"]
            vals = [
                res["tracked"], p.price.display, p.retailer, p.page, p.grade,
                other,
                p.title, "", p.page,
                s2["ss"].price.display if s2.get("ss") else "—",
                s2["ss"].title if s2.get("ss") else "—", "",
                s2["ss"].page if s2.get("ss") else "—",
                verdict_disp,
            ]
        else:
            vals = [res["tracked"], "—", "—", "—", "NO MATCH", "—", "—", "", "—",
                    s2["ss"].price.display if s2.get("ss") else "—",
                    s2["ss"].title if s2.get("ss") else "—", "",
                    s2["ss"].page if s2.get("ss") else "—",
                    s2.get("note", verdict)]
        for i, v in enumerate(vals, start=1):
            cell = ws.cell(r, i, v)
            cell.fill = PatternFill("solid", fgColor=fill_hex)
            cell.font = Font(size=10, color=font_hex, bold=(i in (1, 2, 5, 8, 12)))
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border
        # thumbnails: real flyer image when extraction provided one, else placeholder
        for col, coll in ((8, p), (12, s2.get("ss"))):
            if not coll:
                continue
            tp = os.path.join(tmpdir, "t%d_%d.png" % (r, col))
            real = coll.image and os.path.exists(coll.image)
            try:
                if real:
                    from PIL import Image as PILImage
                    _pil = PILImage.open(coll.image).convert("RGB")
                    _pil.thumbnail((140, 140))
                    _pil.save(tp)
                else:
                    thumb_for(coll.title).save(tp)
            except Exception:
                thumb_for(coll.title).save(tp)
            img = XLImage(tp)
            img.width, img.height = 100, 100
            ws.add_image(img, ws.cell(r, col).coordinate)

    # ---- Sheet 2: Review queue (STRONG) ----
    wq = wb.create_sheet("Review Queue")
    qh = ["Tracked Item", "Candidate", "Retailer", "Price", "Page", "Image", "Grade", "Reason"]
    qw = [28, 34, 13, 16, 7, 16, 10, 60]
    for i, (h, w) in enumerate(zip(qh, qw), start=1):
        cell = wq.cell(1, i, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="404040")
        wq.column_dimensions[cell.column_letter].width = w
    qr = 1
    for res in results:
        for c in res["stage1"]["collisions"]:
            if c.grade != STRONG:
                continue
            qr += 1
            for i, v in enumerate([res["tracked"], c.title, c.retailer,
                                   c.price.display if c.price else "?",
                                   c.page, "", c.grade,
                                   " | ".join(c.reasons)], start=1):
                cell = wq.cell(qr, i, v)
                cell.fill = PatternFill("solid", fgColor="FFEB9C")
                cell.alignment = Alignment(wrap_text=True, vertical="center")
                cell.border = border
            wq.row_dimensions[qr].height = 88
            # thumbnail in the Image column (col 6)
            tp = os.path.join(tmpdir, "q%d.png" % qr)
            real = c.image and os.path.exists(c.image)
            try:
                if real:
                    from PIL import Image as PILImage
                    _pil = PILImage.open(c.image).convert("RGB")
                    _pil.thumbnail((120, 120))
                    _pil.save(tp)
                else:
                    thumb_for(c.title).save(tp)
            except Exception:
                thumb_for(c.title).save(tp)
            qimg = XLImage(tp)
            qimg.width, qimg.height = 80, 80
            wq.add_image(qimg, wq.cell(qr, 6).coordinate)

    # ---- Sheet 3: Audit trail (Step 27, condensed) ----
    wa = wb.create_sheet("Audit")
    ah = ["Tracked Item", "Master Wording", "Allowed Brands", "Master Size",
          "Retailer", "Flyer Wording", "Cand. Brand", "Cand. Size",
          "Advertised Price", "Effective Unit", "Eligibility Class",
          "Match Status", "Match Reason", "Evidence Checked"]
    for i, h in enumerate(ah, start=1):
        cell = wa.cell(1, i, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="404040")
        wa.column_dimensions[cell.column_letter].width = 20
    ar = 1
    for res in results:
        for c in res["stage1"]["collisions"] + res["stage1"]["related"]:
            ar += 1
            m = res["master_parsed"]
            vals = [res["tracked"], m.raw, ", ".join(m.brands_allowed),
                    "%s %s" % (m.size.value or "", m.size.kind),
                    c.retailer, c.title,
                    ", ".join(c.candidate.brands_allowed) if c.candidate else "",
                    "%s %s" % (c.candidate.size.value or "", c.candidate.size.kind) if c.candidate else "",
                    c.price.advertised if c.price else "?",
                    (c.price.effective_each or comparable_unit_price(c.price) or "") if c.price else "",
                    "Class %d" % c.price.eligibility if c.price else "?",
                    c.grade, " | ".join(c.reasons),
                    "title text; size normalized; brand gate; price basis"]
            for i, v in enumerate(vals, start=1):
                cell = wa.cell(ar, i, v)
                cell.alignment = Alignment(wrap_text=True, vertical="center")
                cell.border = border

    wb.save(out_path)
    return out_path

# ------------------------------------------------------------------ main ---

def load_products(xlsx_path: str) -> list[str]:
    from openpyxl import load_workbook
    wb = load_workbook(xlsx_path, read_only=True)
    ws = wb.active
    items = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] and str(row[0]).strip() and not str(row[0]).startswith("#"):
            items.append(str(row[0]).strip())
    return items

def main():
    data = os.path.join(BASE, "data")
    products = load_products(os.path.join(data, "products.xlsx"))
    with open(os.path.join(data, "flyers_sample.json"), encoding="utf-8") as f:
        flyers = json.load(f)["flyers"]
    comp_items, ss_items = [], []
    for fl in flyers:
        for it in fl["items"]:
            rec = {"retailer": fl["retailer"], "title": it["title"],
                   "price": it.get("price", ""), "page": it.get("page", ""),
                   "image": it.get("image", "")}
            (ss_items if fl["retailer"].lower() == "superstore" else comp_items).append(rec)
    print("tracked items: %d | competitor offers: %d | superstore offers: %d"
          % (len(products), len(comp_items), len(ss_items)))
    results = []
    for raw in products:
        masters = parse_item(raw, is_master=True)
        master = masters[0]  # v1: first branch drives; OR-branches share gates
        master.brands_allowed = sorted({b for m in masters for b in m.brands_allowed})
        s1 = stage1(master, comp_items)
        s2 = stage2(master, ss_items, s1["primary"])
        results.append({"tracked": raw, "master_parsed": master,
                        "stage1": s1, "stage2": s2})
    # summary
    from collections import Counter
    vc = Counter(r["stage2"]["verdict"] for r in results)
    print("stage-2 verdicts:", dict(vc))
    n_flag = sum(1 for r in results if r["stage2"]["verdict"] == "MORE_EXPENSIVE")
    print("flags (Superstore MORE EXPENSIVE): %d" % n_flag)
    out = os.path.join(BASE, "output", "price_match_demo.xlsx")
    write_excel(results, out)
    print("wrote", out)

if __name__ == "__main__":
    main()
