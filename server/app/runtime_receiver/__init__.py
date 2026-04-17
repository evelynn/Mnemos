"""OTLP HTTP receiver (spec §7.6) — PII scrubbing baked in."""

from app.runtime_receiver.router import router  # noqa: F401
from app.runtime_receiver.scrub import scrub_span  # noqa: F401
