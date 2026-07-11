#!/usr/bin/env python3
"""
scripts/post_dashboard_event.py
==================================
Automates docs/RUNBOOK_DASHBOARD.md Step2 -> Step3:

    RuntimePipelineOrchestrator.run(raw_event)  ->  POST /aggregate

This script only wires together existing, unmodified components
(RuntimePipelineOrchestrator, EventAggregate) and an HTTP POST. It adds no
new behavior to the Runtime, Verification, Trust, Dashboard, Aggregator, or
FastAPI layers, and does not change the /aggregate request or response
contract.

The raw_event (dict) passed to RuntimePipelineOrchestrator.run() is chosen
with this priority:

    1. --input PATH   -> read raw_event JSON from a file
    2. stdin           -> read raw_event JSON from stdin, if piped in
    3. (neither given) -> use the built-in SAMPLE_RAW_EVENT

Usage:
    python scripts/post_dashboard_event.py

    python scripts/post_dashboard_event.py \\
        --input sample_event.json

    cat sample_event.json \\
        | python scripts/post_dashboard_event.py

    python scripts/post_dashboard_event.py \\
        --url https://xxxxx.run.app \\
        --input sample_event.json
"""

import argparse
import dataclasses
import datetime
import json
import sys
from pathlib import Path

import requests

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from runtime.pipeline_orchestrator import RuntimePipelineOrchestrator  # noqa: E402

# Same shape as the transcript event used in docs/RUNBOOK_DASHBOARD.md Step2.
SAMPLE_RAW_EVENT = {
    "version": 1,
    "type": "transcript",
    "timestamp": "2026-07-11T12:00:00+00:00",
    "payload": {
        "text": "hello there",
        "lang": "en",
        "ts": 1720000000.0,
        "speaker": "user",
    },
}


def _json_default(value):
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _load_raw_event(input_path: str | None) -> dict:
    """Resolve raw_event per the documented priority: --input, then stdin,
    then SAMPLE_RAW_EVENT. Performs no conversion or correction of the
    parsed JSON -- it must already match what
    RuntimePipelineOrchestrator.run() accepts.
    """
    if input_path is not None:
        text = Path(input_path).read_text()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        return SAMPLE_RAW_EVENT

    return json.loads(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8080",
        help="Base URL of the running FastAPI server (default: %(default)s)",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Path to a JSON file containing a raw_event dict. "
        "If omitted, reads from stdin if piped in, otherwise uses a built-in sample event.",
    )
    args = parser.parse_args()

    try:
        raw_event = _load_raw_event(args.input)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: Invalid Runtime Event JSON ({exc})")
        return 1

    outcome = RuntimePipelineOrchestrator().run(raw_event)

    payload = json.loads(
        json.dumps(dataclasses.asdict(outcome.event_aggregate), default=_json_default)
    )

    endpoint = args.url.rstrip("/") + "/aggregate"
    response = requests.post(endpoint, json=payload)

    print(f"POST /aggregate: {response.status_code} {response.reason}")

    if response.ok:
        dashboard_result = outcome.event_aggregate.dashboard_result
        trust_result = outcome.event_aggregate.trust_result
        print(f"Trust Score: {trust_result.trust_score}")
        print(f"Trust Level: {trust_result.trust_level}")
        print(f"Gap Detected: {dashboard_result.gap_detected}")
        print(f"Session ID: {outcome.event_aggregate.session_id}")
    else:
        print(response.text)

    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
