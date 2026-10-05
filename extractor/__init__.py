"""Extractor package: pull flyers from a source into the matcher's input schema."""
from .models import FlyerMeta, FlyerItem
from .base import FlyerSource

__all__ = ["FlyerMeta", "FlyerItem", "FlyerSource"]
