import io
import json
import logging

import pytest

from chassis.correlation import correlation_scope, current_correlation_id, sanitize_correlation_id
from chassis.errors import ConfigError
from chassis.health import HealthRegistry
from chassis.logging import JsonFormatter, configure_logging
from chassis.settings import EnvReader


def test_env_reader_returns_defaults_and_typed_values():
    reader = EnvReader({"PORT": "8080", "RATE": "0.5", "MODE": "fast"})
    assert reader.integer("PORT", 1) == 8080
    assert reader.integer("MISSING", 7) == 7
    assert reader.number("RATE", 1.0) == 0.5
    assert reader.choice("MODE", "slow", ("slow", "fast")) == "fast"
    assert reader.text("NAME", "dflt") == "dflt"
    reader.finish()


def test_env_reader_reports_every_problem_at_once():
    reader = EnvReader({"PORT": "abc", "RATE": "-1", "MODE": "weird", "COUNT": "99"})
    reader.integer("PORT", 1)
    reader.number("RATE", 1.0)
    reader.choice("MODE", "slow", ("slow", "fast"))
    reader.integer("COUNT", 1, maximum=10)
    reader.text("DATABASE_URL")
    with pytest.raises(ConfigError) as caught:
        reader.finish()
    message = str(caught.value)
    for fragment in (
        "PORT must be an integer",
        "RATE must be between",
        "MODE must be one of",
        "COUNT must be between",
        "DATABASE_URL is required",
    ):
        assert fragment in message


def test_env_reader_rejects_a_non_number_float():
    reader = EnvReader({"RATE": "fast"})
    reader.number("RATE", 1.0)
    with pytest.raises(ConfigError):
        reader.finish()


def test_sanitize_correlation_id_keeps_safe_ids_and_replaces_unsafe_ones():
    assert sanitize_correlation_id("abc-123_x.y") == "abc-123_x.y"
    assert sanitize_correlation_id('bad"\nid') != 'bad"\nid'
    assert len(sanitize_correlation_id(None)) == 32


def test_correlation_scope_sets_and_restores_the_id():
    assert current_correlation_id() is None
    with correlation_scope("req-1") as value:
        assert value == "req-1"
        assert current_correlation_id() == "req-1"
    assert current_correlation_id() is None


def test_json_logs_carry_the_correlation_id_and_mask_secrets():
    stream = io.StringIO()
    configure_logging("demo", "INFO", stream)
    with correlation_scope("req-9"):
        logging.getLogger("demo.test").info("hello", extra={"order_id": "o-1", "api_token": "abc"})
    line = json.loads(stream.getvalue())
    assert (line["service"], line["correlation_id"], line["order_id"], line["api_token"], line["level"]) == (
        "demo",
        "req-9",
        "o-1",
        "***",
        "INFO",
    )


def test_json_logs_include_exception_text():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("demo"))
    logger = logging.getLogger("demo.exc")
    logger.handlers[:] = [handler]
    logger.propagate = False
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("failed")
    assert "RuntimeError: boom" in json.loads(stream.getvalue())["exc"]


def test_health_registry_reports_failed_checks_by_name():
    registry = HealthRegistry()
    registry.add("ok", lambda: None)

    def broken():
        raise ConnectionError

    registry.add("broken", broken)
    report = registry.run()
    assert report.ready is False
    assert report.checks == {"ok": "ok", "broken": "failed: ConnectionError"}
