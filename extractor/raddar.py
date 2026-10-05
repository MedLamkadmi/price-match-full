"""Raddar flyer source (raddar.ca + its ADF flyer-viewer backend).

Two data planes (verified 2026-10-04, all unauthenticated):

  A. raddar.ca JSON API
     - GET /api/v2/retailers/?lang=en&postal_code={FSA}      -> retailer directory
     - GET /api/v2/publications/search?deal_name=&retailer_id=&site_id=&lang=en
                                                              -> resolve flyer, get dates
     - GET /api/v2/products/recommended?publication_ids={id}&limit=50
                                                              -> front-page items (fallback)

  B. ADF viewer API (full offer extraction)
     - GET https://app-raddar-adf-frontend-prod.azurewebsites.net/config/app-{fc}.json
                                                              -> {api, apikey, banner_id}
     - GET {api}/offers/{banner}/{deal}/{store}/{lang}/getOffers  (headers below)
                                                              -> ALL offers in the flyer
     Headers: Ocp-Apim-Subscription-Key, Banner, x-api-version.

Per-retailer one-time config in retailers.yaml: `fc`, `site_id`, `deal_code`.
deal_name = wk{ISOweek}-{YYYY}-{deal_code}-IB. Only flyers LAUNCHING in the
target week are returned (not still-active old ones).
"""
import re

from .base import FlyerSource
from .models import FlyerMeta, FlyerItem
from .adf import AdfApi, offers_to_items

BASE_URL = "https://www.raddar.ca"


class RaddarSource(FlyerSource):
    def __init__(self, session, base_url: str = BASE_URL,
                 postal_code: str = "M6B3K4", lang: str = "en"):
        self.s = session
        self.base = base_url.rstrip("/")
        self.postal_code = postal_code.replace(" ", "")
        self.lang = lang
        self._retailers_cache = None
        self._adf_cache = {}

    # ------------------------------------------------------------ listing ---
    def list_flyers(self, week: str, retailers: list[str] | None = None) -> list[FlyerMeta]:
        want_from, want_to = _week_bounds(week)
        found = []
        for cfg in self._retailer_configs(retailers):
            flyer = self._flyer_for_retailer(cfg, week)
            if flyer and _launches_in_week(flyer.valid_from, want_from, want_to):
                found.append(flyer)
            elif flyer:
                print("  -- %s: flyer %s starts %s (outside %s)" % (
                    cfg["name"], flyer.flyer_id, flyer.valid_from, week))
        return found

    def _retailer_configs(self, retailers):
        import os
        import yaml
        cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "config", "retailers.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self._ext_cfg = cfg.get("extractor", {})
        if self._ext_cfg.get("postal_code"):
            self.postal_code = self._ext_cfg["postal_code"].replace(" ", "")
        if self._ext_cfg.get("lang"):
            self.lang = self._ext_cfg["lang"]
        api_rets = self._retailers()
        out = []
        for r in cfg["retailers"]:
            if retailers and r["name"] not in retailers:
                continue
            r = dict(r)
            r["slug"] = self._resolve_slug(api_rets, r["name"], r.get("raddar_slug") or "")
            out.append(r)
        return out

    def _retailers(self) -> list[dict]:
        if self._retailers_cache is None:
            r = self.s.get(self.base + "/api/v2/retailers/",
                           params={"lang": self.lang, "postal_code": self.postal_code})
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                for key in ("retailers", "items", "data", "results"):
                    if isinstance(data.get(key), list):
                        data = data[key]
                        break
            self._retailers_cache = data if isinstance(data, list) else []
        return self._retailers_cache

    @staticmethod
    def _resolve_slug(api_rets, name, hint):
        want = name.lower()
        for r in api_rets:
            rn = str(r.get("name") or r.get("nameEn") or "").lower()
            rs = str(r.get("slug") or "").lower()
            if rn == want or (hint and rs == hint.lower()):
                return r.get("slug") or hint
        return hint or _slugify(name)

    def _flyer_for_retailer(self, cfg: dict, week: str) -> FlyerMeta | None:
        site_id = cfg.get("site_id") or ""
        if not site_id:
            print("  !! %s: site_id not configured (see retailers.yaml)" % cfg["name"])
            return None
        year, ww = week.split("-W")
        # deal names: explicit override (numeric IDs) or template
        deal_names = []
        if cfg.get("deal_name"):
            deal_names.append(cfg["deal_name"])
        template = cfg.get("deal_template") or ""
        if template:
            deal_names.append(template.replace("{WW}", "%d" % int(ww)).replace("{YYYY}", year))
        # legacy fields
        code = cfg.get("deal_code") or ""
        if code and not template:
            deal_names.append("wk%d-%s-%s-IB" % (int(ww), year, code))
        if not deal_names:
            deal_names.append("wk%d-%s" % (int(ww), year))
        for deal_name in deal_names:
            flyer = self._resolve_deal(cfg, deal_name, site_id)
            if flyer:
                return flyer
        # numeric deal IDs (Metro/Food Basics): probe upward from last known
        # (deal IDs increment; the ADF keys authenticate but don't list deals)
        base_deal = cfg.get("deal_name") or ""
        if base_deal.isdigit():
            probe_max = int(cfg.get("deal_probe_max", 40))
            want_from, want_to = _week_bounds(week)
            print("  .. %s: probing deal IDs %s-%s" % (
                cfg["name"], base_deal, int(base_deal) + probe_max))
            for n in range(int(base_deal) + 1, int(base_deal) + probe_max + 1):
                flyer = self._resolve_deal(cfg, str(n), site_id)
                if flyer and _launches_in_week(flyer.valid_from, want_from, want_to):
                    print("  ++ %s: auto-discovered deal %s (%s)" % (
                        cfg["name"], n, flyer.valid_from))
                    return flyer
        print("  !! %s: no flyer found (tried %s)" % (cfg["name"], ", ".join(deal_names)))
        return None

    def _resolve_deal(self, cfg: dict, deal_name: str, site_id: str) -> FlyerMeta | None:
        try:
            r = self.s.get(self.base + "/api/v2/publications/search",
                           params={"deal_name": deal_name, "retailer_id": cfg["slug"],
                                   "site_id": site_id, "lang": self.lang})
            data = r.json()
        except Exception as e:
            print("  !! %s: resolve failed: %s" % (cfg["name"], e))
            return None
        if not isinstance(data, dict) or data.get("type") != "found":
            return None
        pub = data["publication"]
        pub_id = pub.get("id", "")
        vf, vt = self._flyer_dates(pub_id)
        return FlyerMeta(
            retailer=cfg["name"], flyer_id="%s/%s" % (cfg["slug"], deal_name),
            valid_from=vf or _iso(pub.get("previewFrom")),
            valid_to=vt or _iso(pub.get("previewTo")),
            source_url="%s/%s/flyers/x/%s/%s/%s/%s" % (
                self.base, self.lang, cfg["slug"], deal_name, pub_id, site_id),
            extra={"adf": {"fc": cfg.get("fc") or "", "deal": deal_name,
                           "site_id": site_id, "pub_id": pub_id}})

    def _flyer_dates(self, pub_id: str):
        try:
            r = self.s.get(self.base + "/api/v2/products/recommended",
                           params={"publication_ids": pub_id, "limit": 1})
            items = _as_list(r.json())
            if items:
                return items[0].get("validFrom", "") or "", items[0].get("validTo", "") or ""
        except Exception:
            pass
        return "", ""

    # -------------------------------------------------------------- items ---
    def extract_items(self, flyer: FlyerMeta) -> list[FlyerItem]:
        adf = (flyer.extra or {}).get("adf", {})
        if adf.get("fc"):
            try:
                return self._items_via_adf(adf)
            except Exception as e:
                print("  !! %s: ADF failed (%s), falling back to recommended" %
                      (flyer.retailer, str(e)[:80]))
        return self._items_via_recommended(flyer)

    def _adf(self, fc: str) -> AdfApi:
        if fc not in self._adf_cache:
            self._adf_cache[fc] = AdfApi(self.s, fc)
        return self._adf_cache[fc]

    def _items_via_adf(self, adf: dict) -> list[FlyerItem]:
        api = self._adf(adf["fc"])
        offers = api.get_offers(adf["deal"], adf["site_id"], self.lang)
        items = offers_to_items(offers)
        if not items:
            # try the pages endpoint as a second shape
            pages = api.get_pages(adf["deal"], adf["site_id"], self.lang)
            items = offers_to_items(_offers_from_pages(pages))
        return items

    def _items_via_recommended(self, flyer: FlyerMeta) -> list[FlyerItem]:
        """Fallback: front-page items only (50)."""
        m = re.search(r"([a-f0-9]{24})", flyer.source_url or "")
        if not m:
            return []
        try:
            r = self.s.get(self.base + "/api/v2/products/recommended",
                           params={"publication_ids": m.group(1), "limit": 50})
            raw = _as_list(r.json())
        except Exception:
            return []
        items, seen = [], set()
        for it in raw:
            if not (it.get("nameEn") or it.get("name")):
                continue
            item = self._to_item(it)
            key = (item.title.lower(), item.price)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
        return items

    @staticmethod
    def _to_item(it: dict) -> FlyerItem:
        name = it.get("nameEn") or it.get("name") or ""
        desc = it.get("descriptionEn") or it.get("description") or ""
        title = name if (not desc or desc.lower() in name.lower()) else (name + " " + desc).strip()
        price = it.get("priceEn") or it.get("price") or ""
        sku = it.get("sku") or ""
        page = ""
        pm = re.search(r"\.p(\d+)\.", sku)
        if pm:
            page = int(pm.group(1))
        return FlyerItem(
            title=title.strip(), price=str(price).strip(), page=page,
            image_url=it.get("imageUrl") or "", fine_print=desc.strip(),
            category=it.get("universalCategoryEn") or it.get("mainCategoryEn") or "")


# ------------------------------------------------------------------ helpers ---

def _offers_from_pages(pages: dict) -> list:
    """Pull offer dicts out of a pages response (shape-tolerant)."""
    out = []
    if isinstance(pages, dict):
        plist = pages.get("pages") or pages.get("data") or []
    elif isinstance(pages, list):
        plist = pages
    else:
        plist = []
    for p in plist:
        if not isinstance(p, dict):
            continue
        for key in ("offers", "items", "products", "hotspots"):
            for o in p.get(key) or []:
                if isinstance(o, dict):
                    oo = dict(o)
                    oo.setdefault("page", p.get("pageNumber") or p.get("page") or p.get("number") or "")
                    out.append(oo)
    return out


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "products", "offers", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _iso(s: str) -> str:
    return (s or "")[:10]


def _week_bounds(week: str):
    import datetime as dt
    year, w = week.split("-W")
    monday = dt.date.fromisocalendar(int(year), int(w), 1)
    return monday, monday + dt.timedelta(days=6)


def _launches_in_week(valid_from: str, want_from, want_to) -> bool:
    import datetime as dt
    if not valid_from:
        return True
    try:
        d = dt.date.fromisoformat(valid_from[:10])
    except ValueError:
        return True
    return want_from <= d <= want_to
