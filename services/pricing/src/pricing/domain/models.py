"""Price rules and the quote value object."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from decimal import Decimal

LOW_STOCK = 10
SURGE = Decimal("1.15")


def base_price(sku: str) -> Decimal:
    """A stable base price for a sku (5.00 to 44.99), derived from the sku so no price table is needed."""
    return (Decimal(5) + Decimal(zlib.crc32(sku.encode()) % 4000) / 100).quantize(Decimal("0.01"))


def price_for(sku: str, quantity: int) -> Decimal:
    """The base price, 15 percent higher when fewer than ten units are left."""
    price = base_price(sku)
    return (price * SURGE).quantize(Decimal("0.01")) if quantity < LOW_STOCK else price


@dataclass(frozen=True, slots=True)
class Quote:
    """A price for a sku. ``degraded`` means the stock store was not consulted for this answer;
    ``stale`` means the price comes from an earlier answer or from the base price alone."""

    sku: str
    price: Decimal
    in_stock: bool | None
    degraded: bool = False
    stale: bool = False


def quote_from_stock(sku: str, quantity: int) -> Quote:
    """A fresh quote from a stock reading."""
    return Quote(sku, price_for(sku, quantity), quantity > 0)


def fallback_quote(sku: str, last_known: Quote | None) -> Quote:
    """The degraded answer when the stock store cannot be asked: the last known quote, or the base price."""
    if last_known is not None:
        return Quote(sku, last_known.price, last_known.in_stock, degraded=True, stale=True)
    return Quote(sku, base_price(sku), None, degraded=True, stale=True)
