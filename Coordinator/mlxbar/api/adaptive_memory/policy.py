"""Adaptive Policy v1 -- deterministic, no learning.

Given the compressible middle of a conversation (already stripped of the
leading system turns and the verbatim tail), decide per message:

* ``exact``  -- keep the message verbatim
* ``latent`` -- replace with a short note
* ``cold``   -- drop from inference (recoverable from the raw message)

The three presets only move two dials: how far back a LATENT candidate has to
be before it is dropped to COLD, and (memorySaver only) whether a short recent
discussion turn is worth a note at all.
"""

from __future__ import annotations

from . import segment

POLICY_VERSION = "am1"

# recent_latent_keep: newest N latent candidates stay LATENT; older -> COLD.
_PRESETS = {
    "fidelity": {"recent_latent_keep": 24, "min_latent_chars": 0},
    "balanced": {"recent_latent_keep": 10, "min_latent_chars": 0},
    "memorySaver": {"recent_latent_keep": 4, "min_latent_chars": 240},
}


def preset(name: str) -> dict:
    return _PRESETS.get(name, _PRESETS["balanced"])


def label_messages(middle: list[dict], *, policy: str, verbatim_protection: bool) -> list[str]:
    """Return a label per message in ``middle`` (same length, same order)."""
    tuning = preset(policy)
    base = [segment.classify(message, verbatim_protection=verbatim_protection) for message in middle]
    latent_positions = [index for index, label in enumerate(base) if label == "latent"]
    keep = set(latent_positions[-tuning["recent_latent_keep"]:]) if tuning["recent_latent_keep"] else set()
    labels: list[str] = []
    for index, label in enumerate(base):
        if label != "latent":
            labels.append(label)
            continue
        text = segment.message_text(middle[index])
        if index not in keep:
            labels.append("cold")
        elif tuning["min_latent_chars"] and len(text) < tuning["min_latent_chars"]:
            labels.append("cold")
        else:
            labels.append("latent")
    return labels
