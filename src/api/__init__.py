"""
api
=====
H4-6 Presentation Layer — a read-only, stateless HTTP surface over the
EventAggregate produced downstream by the H4-5 Event Aggregator.

This package never imports or invokes the Cloud Run Runtime, any Provider,
Whisper, the Verification Runtime, the Trust Runtime, the Dashboard
Runtime, or the Event Aggregator. See api_server.py and api_models.py for
the full scope statement.
"""
