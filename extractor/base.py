"""Source interface. A new flyer source = one subclass, no matcher changes."""
from abc import ABC, abstractmethod
from .models import FlyerMeta


class FlyerSource(ABC):
    """A place flyers come from (Raddar, Flipp, PDF folder, ...)."""

    @abstractmethod
    def list_flyers(self, week: str, retailers: list[str] | None = None) -> list[FlyerMeta]:
        """Flyers LAUNCHING in `week` (YYYY-Www), optionally filtered to retailers.
        Only metadata is fetched here — items come from extract_items."""

    @abstractmethod
    def extract_items(self, flyer: FlyerMeta) -> list:
        """All advertised items in one flyer -> list[FlyerItem]."""

    def extract_week(self, week: str, retailers: list[str] | None = None) -> list[FlyerMeta]:
        flyers = self.list_flyers(week, retailers)
        for f in flyers:
            f.items = self.extract_items(f)
        return flyers
