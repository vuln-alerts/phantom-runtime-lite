#!/usr/bin/env python3
"""
scripts/build_dashboard_event.py
===================================
Converts one row of an already-persisted Runtime transcript JSONL file
(written after Runtime exits, or while it runs, by
transcript.persistence.persist_entry() -- src/transcript/persistence.py,
enabled via ENABLE_TRANSCRIPT_PERSIST=1 SESSION_OUTPUT_DIR=<dir>, see
phantom_runtime.py's own module docstring "EXAMPLE COMMAND") into one
metadata-included raw_event JSON file, suitable for
scripts/post_dashboard_event.py --input.

This script does not run Runtime, Verification, Trust, Dashboard, or the
Pipeline, and does not talk to any API -- it only reshapes one already-
recorded JSONL row into the raw_event shape RuntimePipelineOrchestrator.run()
accepts (docs/H4_RUNTIME_EVENT_CONTRACT.md "Runtime Event Metadata").
scripts/post_dashboard_event.py itself is unmodified and unaware this
script exists.

JSONL row schema (verbatim, from persist_entry()'s docstring):
    type, session_id, ts, speaker, lang, text, state, latency_ms

No text, speaker, or timing is fabricated -- every payload/metadata field
below is copied verbatim from one selected row. conversation_line is that
row's 1-based line number within the JSONL file (persist_entry() appends
one row per utterance in the order Runtime Conversation utterances
occurred, so file line number IS the utterance/conversation number).

Usage:
    python scripts/build_dashboard_event.py \\
        --session-dir ./sessions --output generated.json

    python scripts/build_dashboard_event.py \\
        --transcript ./sessions/transcript_20260712_120000.jsonl \\
        --line 3 --output generated.json
"""

import argparse
import datetime
import glob
import json
import os
import sys


def _latest_transcript(session_dir: str) -> str:
    candidates = sorted(glob.glob(os.path.join(session_dir, "transcript_*.jsonl")))
    if not candidates:
        raise SystemExit(f"ERROR: no transcript_*.jsonl found under {session_dir}")
    return candidates[-1]


def _load_lines(transcript_path: str) -> list:
    """Read the JSONL file, keeping each non-blank line's 1-based file line
    number as conversation_line. Only 'utterance' rows are eligible for
    selection -- persist_entry() writes no other row type today, but this
    guards against silently picking a future non-utterance row type."""
    lines = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            row = json.loads(raw_line)
            if row.get("type") == "utterance":
                lines.append((line_no, row))
    if not lines:
        raise SystemExit(f"ERROR: no utterance rows found in {transcript_path}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", default="./sessions",
                         help="Directory containing transcript_*.jsonl files (default: %(default)s)")
    parser.add_argument("--transcript", default=None,
                         help="Explicit path to a transcript_*.jsonl file (overrides --session-dir)")
    parser.add_argument("--line", type=int, default=None,
                         help="1-based JSONL line number to use (default: last utterance in the file)")
    parser.add_argument("--output", default="generated.json",
                         help="Output raw_event JSON path (default: %(default)s)")
    args = parser.parse_args()

    transcript_path = args.transcript or _latest_transcript(args.session_dir)
    lines = _load_lines(transcript_path)

    if args.line is not None:
        matches = [(n, r) for n, r in lines if n == args.line]
        if not matches:
            raise SystemExit(f"ERROR: line {args.line} is not an utterance row in {transcript_path}")
        conversation_line, row = matches[0]
    else:
        conversation_line, row = lines[-1]

    raw_event = {
        "version": 1,
        "type": "transcript",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "payload": {
            "text": row["text"],
            "speaker": row["speaker"],
            "ts": row["ts"],
            "lang": row["lang"],
        },
        "metadata": {
            "conversation_line": conversation_line,
            "speaker": row["speaker"],
            "transcript": row["text"],
        },
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(raw_event, f, ensure_ascii=False, indent=2)

    print(f"Wrote {args.output} (from {transcript_path}, line {conversation_line})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
