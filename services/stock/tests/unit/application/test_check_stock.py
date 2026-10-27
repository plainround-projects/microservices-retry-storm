import asyncio

import pytest

from resilience.deadline import Deadline
from resilience.errors import DeadlineExceeded
from stock.application.check_stock import CheckStock
from stock.domain.models import Faults, InjectedFailure, InvalidFaults, UnknownSku


class Repo:
    def __init__(self, levels):
        self.levels = levels

    async def quantity(self, sku):
        return self.levels.get(sku)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make(levels=None, faults=None, capacity=4, rand=0.5, sleep=None):
    faults = faults or Faults(0, 0.0)
    clock = Clock()

    async def default_sleep(seconds):
        await asyncio.sleep(0)

    use_case = CheckStock(
        Repo(levels or {"sku-001": 5}),
        lambda: faults,
        asyncio.Semaphore(capacity),
        clock=clock,
        sleep=sleep or default_sleep,
        rand=lambda: rand,
    )
    return use_case, clock


async def test_a_known_sku_returns_its_quantity_and_whether_it_is_in_stock():
    use_case, _ = make({"a": 5, "b": 0})
    assert (await use_case("a", None)).in_stock is True
    assert (await use_case("b", None)).in_stock is False
    assert (await use_case("a", None)).quantity == 5


async def test_an_unknown_sku_is_reported():
    use_case, _ = make()
    with pytest.raises(UnknownSku):
        await use_case("ghost", None)


async def test_the_fail_rate_decides_which_calls_fail():
    failing, _ = make(faults=Faults(0, 0.3), rand=0.29)
    passing, _ = make(faults=Faults(0, 0.3), rand=0.31)
    with pytest.raises(InjectedFailure):
        await failing("sku-001", None)
    assert (await passing("sku-001", None)).quantity == 5


async def test_the_configured_latency_is_waited():
    waited = []

    async def sleep(seconds):
        waited.append(seconds)

    use_case, _ = make(faults=Faults(1500, 0.0), sleep=sleep)
    await use_case("sku-001", None)
    assert waited == [1.5]


async def test_a_request_with_a_deadline_gives_up_when_the_deadline_passes_during_the_work():
    use_case, clock = make(faults=Faults(5000, 0.0), sleep=asyncio.sleep)
    with pytest.raises(DeadlineExceeded):
        await use_case("sku-001", Deadline.after(clock(), 0.05))


async def test_a_request_with_an_expired_deadline_is_refused_before_any_work():
    use_case, clock = make()
    with pytest.raises(DeadlineExceeded):
        await use_case("sku-001", Deadline.after(clock(), 0.0))


async def test_a_request_with_a_deadline_stops_waiting_for_capacity_when_the_deadline_passes():
    gate = asyncio.Semaphore(1)
    clock = Clock()
    use_case = CheckStock(Repo({"s": 1}), lambda: Faults(0, 0.0), gate, clock=clock, rand=lambda: 0.5)
    await gate.acquire()
    with pytest.raises(DeadlineExceeded):
        await use_case("s", Deadline.after(clock(), 0.05))
    gate.release()


async def test_a_request_without_a_deadline_waits_for_capacity_as_long_as_it_takes():
    gate = asyncio.Semaphore(1)
    use_case = CheckStock(Repo({"s": 1}), lambda: Faults(0, 0.0), gate, clock=Clock(), rand=lambda: 0.5)
    await gate.acquire()
    task = asyncio.create_task(use_case("s", None))
    await asyncio.sleep(0.05)
    assert not task.done()
    gate.release()
    assert (await task).quantity == 1


@pytest.mark.parametrize("latency", [-1, 60_001])
def test_faults_reject_an_out_of_range_latency(latency):
    with pytest.raises(InvalidFaults):
        Faults(latency, 0.0)


@pytest.mark.parametrize("rate", [-0.1, 1.1])
def test_faults_reject_an_out_of_range_fail_rate(rate):
    with pytest.raises(InvalidFaults):
        Faults(0, rate)
