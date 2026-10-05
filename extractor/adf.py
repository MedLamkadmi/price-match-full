"""ADF flyer-viewer API client (the engine behind raddar.ca's flyer viewer).

Per-retailer config comes from the viewer's public config endpoint:
    GET https://app-raddar-adf-frontend-prod.azurewebsites.net/config/app-{fc}.json
        -> {api, apikey, banner_id, api_version}

Then, with headers {Ocp-Apim-Subscription-Key, Banner, x-api-version}:
    GET {api}/pages/{dealName}/{storeId}/{lang}/   -> pages + positioned offers
    GET {api}/offers/{banner}/{deal}/{store}/{lang}/getOffers  -> flat offer list

These are the same public endpoints the site's own viewer calls — no scraping
of rendered pages, no OCR. Polite delays apply via the shared session.
"""
import re

VIEWER_BASE = "https://app-raddar-adf-frontend-prod.azurewebsites.net"


class AdfApi:
    def __init__(self, session, fc: str):
        self.s = session
        self.fc = fc
        self._cfg = None

    def config(self) -> dict:
        if self._cfg is None:
            r = self.s.get("%s/config/app-%s.json" % (VIEWER_BASE, self.fc), timeout=30)
            r.raise_for_status()
            self._cfg = r.json()
        return self._cfg

    def _headers(self) -> dict:
        cfg = self.config()
        return {
            "Ocp-Apim-Subscription-Key": cfg["apikey"],
            "Banner": cfg["banner_id"],
            "x-api-version": cfg.get("api_version", "3.0"),
            "Content-Type": "application/json;charset=UTF-8",
        }

    def get_pages(self, deal_name: str, store_id: str, lang: str = "en") -> dict:
        """Full flyer: pages with positioned offers."""
        cfg = self.config()
        url = "%s/pages/%s/%s/%s/" % (cfg["api"].rstrip("/"), deal_name, store_id, lang)
        # polite delay is handled by the session wrapper
        r = self.s.get(url, headers=self._headers(), timeout=60)
        r.raise_for_status()
        return r.json()

    def get_offers(self, deal_name: str, store_id: str, lang: str = "en") -> list:
        """Flat offer list for the flyer."""
        cfg = self.config()
        url = "%s/offers/%s/%s/%s/%s/getOffers" % (
            cfg["api"].rstrip("/"), cfg["banner_id"], deal_name, store_id, lang)
        r = self.s.get(url, headers=self._headers(), timeout=60)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        for key in ("offers", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return []


def offers_to_items(offers: list) -> list:
    """Normalize ADF offer dicts toward FlyerItem fields (shape-tolerant)."""
    from .models import FlyerItem
    items = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        title = (o.get("title") or o.get("name") or o.get("desc") or "").strip()
        if not title:
            continue
        price = str(o.get("price") or o.get("promoPrice") or o.get("priceEn") or "").strip()
        page = o.get("page") or o.get("pageNumber") or o.get("page_number") or ""
        img = o.get("imageUrl") or o.get("image") or o.get("productImage") or ""
        items.append(FlyerItem(title=title, price=price, page=page,
                               image_url=img if isinstance(img, str) else "",
                               fine_print=str(o.get("description") or o.get("bodyEn") or ""),
                               category=str(o.get("category") or "")))
    return items
