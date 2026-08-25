"""JSON logs must keep extra= fields used by AI turn metrics."""

import json
import logging

from tourism_backend.logging_config import JsonFormatter


def test_json_formatter_passes_ai_turn_metrics_extra() -> None:
    record = logging.LogRecord(
        name="tourism_backend.modules.route_builder.application.session_service",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ai_chat_turn",
        args=(),
        exc_info=None,
    )
    record.provider = "lmstudio"
    record.latency_ms = 42
    record.structured_parse = "fallback"
    record.tools_round = 1
    record.rag_hit = False
    record.outage_fallback = True

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "ai_chat_turn"
    assert payload["provider"] == "lmstudio"
    assert payload["latency_ms"] == 42
    assert payload["structured_parse"] == "fallback"
    assert payload["tools_round"] == 1
    assert payload["rag_hit"] is False
    assert payload["outage_fallback"] is True


def test_ai_chat_turn_logger_extra_survives_json_formatter() -> None:
    from tourism_backend.modules.route_builder.application.session_service import (
        _log_ai_chat_turn,
    )

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("tourism_backend.modules.route_builder.application.session_service")
    handler = _Capture()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        _log_ai_chat_turn(
            provider="lmstudio",
            latency_ms=10,
            structured_parse="ok",
            tools_round=0,
            rag_hit=True,
            outage_fallback=False,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert captured
    payload = json.loads(JsonFormatter().format(captured[0]))
    assert payload["message"] == "ai_chat_turn"
    assert payload["provider"] == "lmstudio"
    assert payload["structured_parse"] == "ok"
    assert payload["rag_hit"] is True
    assert payload["outage_fallback"] is False
