"""Data models for the extraction stage.

The extractor's ONLY contract with the matcher is a list of FlyerMeta,
each carrying its FlyerItems. The matcher never talks to the source site.
"""
from dataclasses import dataclass, field


@dataclass
class FlyerItem:
    title: str                    # product title exactly as advertised
    price: str                    # raw price text, e.g. "2/$7", "Member $3.99 / Non-member $4.99"
    page: object = ""             # page number in the flyer (audit trail)
    image_url: str = ""           # product image URL (downloaded to cache by images.py)
    image_path: str = ""          # local cached path, filled by images.py
    fine_print: str = ""          # size/condition notes shown with the offer
    category: str = ""            # source-provided category, if any

    def to_dict(self):
        return {"title": self.title, "price": self.price, "page": self.page,
                "image": self.image_path or self.image_url,
                "fine_print": self.fine_print, "category": self.category}


@dataclass
class FlyerMeta:
    retailer: str                 # canonical name from retailers.yaml
    flyer_id: str                 # stable id for this flyer issue
    valid_from: str = ""          # YYYY-MM-DD
    valid_to: str = ""            # YYYY-MM-DD
    source_url: str = ""          # where it was pulled from (audit)
    items: list = field(default_factory=list)  # list[FlyerItem]
    extra: dict = field(default_factory=dict)  # source-specific params (not in to_dict)

    def to_dict(self):
        return {"retailer": self.retailer, "flyer_id": self.flyer_id,
                "valid_from": self.valid_from, "valid_to": self.valid_to,
                "source_url": self.source_url,
                "items": [i.to_dict() for i in self.items]}
