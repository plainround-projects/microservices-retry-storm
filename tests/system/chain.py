"""The four real applications wired to each other in one process (no containers, no network).

Each hop's HTTP client talks to the next application through an ASGI transport, so the real middleware, the real use
cases and the real policy objects run; only the stock store's database is replaced by a dictionary.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx

from chassis.health import HealthRegistry
from chassis.metrics import ServiceMetrics
from chassis.tracing import SpanStore
from chassis.upstream import HopClient
from gateway.application.handle_search import HandleSearch
from gateway.infrastructure.http_search import HttpSearchUpstream
from gateway.infrastructure.telemetry import PrometheusTelemetry as GatewayTelemetry
from gateway.interfaces.api import create_app as create_gateway
from pricing.application.quote_price import QuotePrice
from pricing.infrastructure.cache import InMemoryQuoteCache
from pricing.infrastructure.http_stock import HttpStockUpstream
from pricing.infrastructure.telemetry import PrometheusTelemetry as PricingTelemetry
from pricing.interfaces.api import create_app as create_pricing
from resilience.backoff import Backoff
from resilience.breaker import CircuitBreaker
from resilience.limiter import ConcurrencyLimiter
from resilience.profile import Profile, ProfileHolder
from resilience.retry_budget import RetryBudget
from search.application.search_products import SearchProducts
from search.infrastructure.http_pricing import HttpPricingUpstream
from search.infrastructure.telemetry import PrometheusTelemetry as SearchTelemetry
from search.interfaces.api import create_app as create_search
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults
from stock.infrastructure.fault_state import FaultState
from stock.interfaces.api import create_app as create_stock


class DictStock:
    """The stock repository, in memory: 20 skus, all in stock."""

    async def quantity(self, sku: str) -> int | None:
        return 20 if sku.startswith("sku-") else None


@dataclass
class Chain:
    """The four applications, their metrics and stores, the shared profile switch and the stock faults."""

    gateway: httpx.AsyncClient
    stores: dict[str, SpanStore]
    metrics: dict[str, ServiceMetrics]
    faults: FaultState
    profiles: dict[str, ProfileHolder] = field(default_factory=dict)
    breaker: CircuitBreaker | None = None
    limiter: ConcurrencyLimiter | None = None

    def set_profile(self, profile: Profile) -> None:
        for holder in self.profiles.values():
            holder.set(profile)


def build_chain(*, faults: Faults | None = None, search_limit: int = 40, capacity: int = 16, seed: int = 1) -> Chain:
    rng = random.Random(seed)
    metrics = {name: ServiceMetrics(name) for name in ("gateway", "search", "pricing", "stock")}
    stores = {name: SpanStore() for name in metrics}
    fault_state = FaultState(faults or Faults(0, 0.0))

    stock_app = create_stock(
        CheckStock(DictStock(), fault_state.get, asyncio.Semaphore(capacity), clock=time.monotonic, rand=rng.random),
        fault_state,
        HealthRegistry(),
        metrics["stock"],
        stores["stock"],
    )

    def client(app: object, caller: str, target: str) -> HopClient:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        inner = httpx.AsyncClient(transport=transport, base_url=f"http://{target}")
        return HopClient(
            caller, target, f"http://{target}", metrics=metrics[caller], store=stores[caller], client=inner
        )

    holders = {name: ProfileHolder() for name in ("gateway", "search", "pricing")}
    breaker = CircuitBreaker(time.monotonic, failure_ratio=0.5, min_calls=10, window_seconds=10.0, open_seconds=5.0)
    pricing_app = create_pricing(
        QuotePrice(
            HttpStockUpstream(client(stock_app, "pricing", "stock")),
            InMemoryQuoteCache(time.monotonic),
            breaker,
            holders["pricing"].get,
            time.monotonic,
            PricingTelemetry(metrics["pricing"]),
        ),
        holders["pricing"],
        HealthRegistry(),
        metrics["pricing"],
        stores["pricing"],
    )
    limiter = ConcurrencyLimiter(search_limit)
    search_app = create_search(
        SearchProducts(
            HttpPricingUpstream(client(pricing_app, "search", "pricing")),
            limiter,
            holders["search"].get,
            time.monotonic,
            SearchTelemetry(metrics["search"]),
        ),
        holders["search"],
        HealthRegistry(),
        metrics["search"],
        stores["search"],
    )
    gateway_app = create_gateway(
        HandleSearch(
            HttpSearchUpstream(client(search_app, "gateway", "search")),
            holders["gateway"].get,
            RetryBudget(time.monotonic, ratio=0.1, window_seconds=10.0, min_retries=3),
            Backoff(base=0.001, cap=0.002),
            time.monotonic,
            GatewayTelemetry(metrics["gateway"]),
        ),
        holders["gateway"],
        HealthRegistry(),
        metrics["gateway"],
        stores["gateway"],
        sample_rate=0.0,
    )
    gateway = httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="http://gateway", timeout=30)
    return Chain(gateway, stores, metrics, fault_state, holders, breaker, limiter)


def received(chain: Chain, service: str) -> float:
    routes = {"gateway": "/search", "search": "/search", "pricing": "/price", "stock": "/stock"}
    return float(chain.metrics[service].received.labels(routes[service])._value.get())
