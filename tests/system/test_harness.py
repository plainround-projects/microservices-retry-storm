"""The measuring tools themselves: window statistics, trace rendering and the recovery rule."""

from typing import Any

from tests.system import harness
from tests.system.experiments import _recovered


def recorder(samples):
    r = harness.LoadRecorder(t0=100.0)
    r.samples = [
        harness.Sample(100.0 + start, latency, status, degraded) for start, latency, status, degraded in samples
    ]
    return r


def test_window_statistics_count_errors_and_degraded_answers_separately_and_use_the_requested_window():
    r = recorder(
        [
            (0.5, 0.1, 200, False),
            (1.0, 0.2, 200, True),
            (1.5, 2.0, 503, False),
            (2.5, 0.3, 200, False),
            (9.0, 5.0, 0, False),
        ]
    )
    window = r.window(0, 2)
    assert window["requests"] == 3
    assert window["error_rate"] == round(1 / 3, 4)
    assert window["degraded_rate"] == round(1 / 3, 4)
    assert window["p99_ms"] == 2000
    assert window["statuses"] == {200: 2, 503: 1}
    assert r.window(8, 10)["statuses"] == {0: 1}


def test_an_empty_window_reports_none_instead_of_dividing_by_zero():
    assert recorder([]).window(0, 10)["requests"] == 0
    assert recorder([]).window(0, 10)["p99_ms"] is None


def test_the_trace_tree_indents_children_under_their_parents_and_shows_the_attempt():
    spans: list[dict[str, Any]] = [
        {
            "spanId": "a",
            "parentId": None,
            "name": "gateway /search",
            "startMs": 1000.0,
            "durationMs": 50.0,
            "status": "200",
            "attributes": {},
        },
        {
            "spanId": "b",
            "parentId": "a",
            "name": "call search",
            "startMs": 1001.0,
            "durationMs": 40.0,
            "status": "200",
            "attributes": {"attempt": 1},
        },
        {
            "spanId": "c",
            "parentId": "b",
            "name": "search /search",
            "startMs": 1002.0,
            "durationMs": 30.0,
            "status": "200",
            "attributes": {},
        },
    ]
    lines = harness.render_trace(spans).splitlines()
    assert [len(line) - len(line.lstrip()) for line in lines] == [0, 2, 4]
    assert "attempt=1" in lines[1]
    assert harness.render_trace([]) == "(no spans recorded)"


def test_the_recovery_rule_needs_clean_fast_answers_and_a_drained_stock_store():
    good = recorder([(t / 2, 0.05, 200, False) for t in range(0, 40)])
    flights_low = [(t / 2, 2.0) for t in range(0, 40)]
    assert _recovered(good, flights_low, 20.0, threshold=10.0)
    assert _recovered(
        recorder([(t / 2, 0.05, 200, True) for t in range(0, 40)]),
        flights_low,
        20.0,
        threshold=10.0,
        baseline={"degraded_rate": 0.9},
    )
    assert not _recovered(good, [(t / 2, 50.0) for t in range(0, 40)], 20.0, threshold=10.0)
    assert not _recovered(good, [], 20.0, threshold=10.0)
    assert not _recovered(recorder([(t / 2, 0.05, 200, True) for t in range(0, 40)]), flights_low, 20.0, threshold=10.0)
    assert not _recovered(recorder([(t / 2, 0.9, 200, False) for t in range(0, 40)]), flights_low, 20.0, threshold=10.0)
    assert not _recovered(
        recorder([(t / 2, 0.05, 503 if t in (28, 30) else 200, False) for t in range(0, 40)]),
        flights_low,
        20.0,
        threshold=10.0,
    )


def test_metric_lookup_builds_the_key_from_sorted_labels():
    values = {"calls_received_total{route=/stock}": 7.0, "in_flight": 3.0}
    assert harness.metric(values, "calls_received_total", route="/stock") == 7.0
    assert harness.metric(values, "in_flight") == 3.0
    assert harness.metric(values, "missing") == 0.0


def test_the_recovery_rule_accepts_a_slower_p99_when_the_baseline_was_slower():
    slow_but_clean = recorder([(t / 2, 0.9, 200, False) for t in range(0, 40)])
    flights = [(t / 2, 2.0) for t in range(0, 40)]
    assert not _recovered(slow_but_clean, flights, 20.0, threshold=10.0)
    assert _recovered(slow_but_clean, flights, 20.0, threshold=10.0, p99_limit_ms=1800)
