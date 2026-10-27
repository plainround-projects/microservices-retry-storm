"""Lesson step 1 made executable: docs/design/policy.md against the numbers in the code."""

import re
from pathlib import Path

from gateway.application import handle_search
from pricing.application import quote_price
from search.application import search_products

DOC = (Path(__file__).resolve().parents[2] / "docs" / "design" / "policy.md").read_text()
TABLE = DOC.split("<!-- policy-table:begin -->")[1].split("<!-- policy-table:end -->")[0]
ROWS = [
    [cell.strip() for cell in line.strip().strip("|").split("|")]
    for line in TABLE.splitlines()
    if line.startswith("|") and not line.startswith("| ---") and not line.startswith("| Hop")
]
DEADLINE_MS = int(re.search(r"\*\*(\d+) ms\*\*", DOC).group(1))  # type: ignore[union-attr]
MARGIN_MS = int(re.findall(r"\*\*(\d+) ms\*\*", DOC)[1])
MIN_USEFUL_MS = int(re.findall(r"\*\*(\d+) ms\*\*", DOC)[2])
TIMEOUTS_MS = {row[0]: int(row[1]) for row in ROWS}


def test_the_per_hop_timeouts_add_up_to_less_than_the_end_to_end_deadline_and_nest():
    gateway, search, pricing = (TIMEOUTS_MS[h] for h in ("gateway to search", "search to pricing", "pricing to stock"))
    assert gateway < DEADLINE_MS
    assert search + MARGIN_MS <= gateway
    assert pricing + MARGIN_MS <= search


def test_exactly_one_layer_is_allowed_to_retry_and_it_is_the_gateway():
    retrying = [row[0] for row in ROWS if row[2].lower().startswith("yes")]
    assert retrying == ["gateway to search"]


def test_every_hop_says_what_it_returns_when_its_dependency_is_down():
    assert all(len(row[3]) > 10 for row in ROWS)


def test_the_numbers_in_the_code_are_the_numbers_on_the_page():
    assert round(handle_search.END_TO_END_DEADLINE * 1000) == DEADLINE_MS
    assert round(handle_search.HOP_TIMEOUT * 1000) == TIMEOUTS_MS["gateway to search"]
    assert round(search_products.HOP_TIMEOUT * 1000) == TIMEOUTS_MS["search to pricing"]
    assert round(quote_price.HOP_TIMEOUT * 1000) == TIMEOUTS_MS["pricing to stock"]
    for module in (search_products, quote_price):
        assert round(module.MARGIN * 1000) == MARGIN_MS
        assert round(module.MIN_USEFUL * 1000) == MIN_USEFUL_MS
