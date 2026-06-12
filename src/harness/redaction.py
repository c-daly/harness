"""Redaction seam (day-one, design lines 93–95). v1 ships the SEAMS and identity defaults;
pattern config is a later phase.

Scrub story (documented cost): a redacted log loses byte-exact replay of redacted tool
results — the folded model transcript contains the masked text, not the original. Redaction
runs BEFORE blob-spill (blobs are content-addressed/immutable, so redact-before-put is the only
sound order) and at Session.append (the event-write choke point covering log AND live UI).
"""

from typing import Callable

from harness.events import Event

# A string redactor for the dispatcher (applied to raw tool output before spill).
StringRedactor = Callable[[str], str]
# An event redactor for Session.append (rebuilds frozen events via model_copy).
EventRedactor = Callable[[Event], Event]


def identity_redact(text: str) -> str:
    return text
