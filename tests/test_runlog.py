"""Run logging: JSON lines with the run's context, secrets masked, and a bounded event buffer."""

import io
import json
from datetime import datetime

from openmetadatahub.runlog import REDACTED, RunEventBuffer, configure_run_logging, logger, redact_mapping, redact_text


def test_tokens_and_secret_values_are_masked():
    text = "token dapi" + "0" * 32 + "-2 and Bearer abcdefghijklmnop and password=hunter2"
    assert redact_text(text) == f"token {REDACTED} and Bearer {REDACTED} and password={REDACTED}"
    assert redact_mapping({"client_secret": "x", "note": "api_key: k", "rows": 3}) == {
        "client_secret": REDACTED, "note": f"api_key: {REDACTED}", "rows": 3}


def test_every_line_is_json_with_the_run_context_and_is_buffered():
    stream = io.StringIO()
    events = configure_run_logging({"run_id": "42", "job": "omh-audit-access"}, stream=stream)
    logger.info("audited {} tables", 3, catalog="main")
    record = json.loads(stream.getvalue().splitlines()[0])["record"]
    assert record["message"] == "audited 3 tables"
    assert record["extra"] == {"run_id": "42", "job": "omh-audit-access", "catalog": "main"}
    assert [(e.seq, e.level, e.context["catalog"]) for e in events.events] == [(0, "INFO", "main")]
    assert type(events.events[0].logged_at) is datetime  # Spark rejects loguru's datetime subclass


def test_the_buffer_stops_at_its_limit():
    events = configure_run_logging({"run_id": "1"}, stream=io.StringIO(), buffer=RunEventBuffer("1", max_events=2))
    for n in range(5):
        logger.info("event {}", n)
    assert len(events.events) == 2 and events.dropped == 3
