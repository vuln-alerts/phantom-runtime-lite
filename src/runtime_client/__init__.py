"""
runtime_client
===============
Mac-side Runtime Client for Phantom Runtime Lite's Cloud Run Runtime.

Captures audio from any local input device (built-in mic, USB mic, or a
virtual loopback device such as BlackHole/Loopback fed by Zoom/Meet/Teams/
Discord), streams it to the Cloud Run Runtime's WebSocket endpoint, and
renders the Typed Events the Runtime sends back. STT, LLM, Meeting
Analysis, Summary, and Memory all remain Server responsibilities; this
package owns only Audio Capture -> PCM16 -> WebSocket -> Typed Event
Display, plus the Keyboard UX ported from phantom-conversational-runtime.
"""
