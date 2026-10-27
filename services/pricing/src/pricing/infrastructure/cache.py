"""An in-memory cache of the last good quote per sku."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable

from pricing.domain.models import Quote


class InMemoryQuoteCache:
    """Remembers up to ``max_entries`` quotes for ``ttl_seconds`` each. A production system would use a shared cache."""

    def __init__(self, clock: Callable[[], float], ttl_seconds: float = 3600.0, max_entries: int = 10_000) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[str, tuple[float, Quote]] = OrderedDict()

    def get(self, sku: str) -> Quote | None:
        """The last known quote, or ``None`` when absent or older than the time to live."""
        entry = self._entries.get(sku)
        if entry is None or self._clock() - entry[0] > self._ttl:
            return None
        return entry[1]

    def put(self, quote: Quote) -> None:
        """Remember a fresh quote, evicting the oldest entry when full."""
        self._entries[quote.sku] = (self._clock(), quote)
        self._entries.move_to_end(quote.sku)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
