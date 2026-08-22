#!/usr/bin/env python3
"""Report what prompt reuse each local model can get, and what it will cost.

Reads only `config.json` -- no weights are loaded -- so a 27 GB model costs the
same as a 9 GB one to check. Run it after downloading a model, or after a
runtime update, to see which models are about to behave differently.

    python3 scripts/cache-capability-matrix.py ~/.lmstudio/models
    python3 scripts/cache-capability-matrix.py --limit-gb 16 ~/.lmstudio/models

Two caveats, stated because acting on the wrong one wastes an afternoon:

* The rollback column is *predicted* from the layer list. The authoritative
  answer comes from the probe MLXBar runs when the model is loaded, which asks
  the cache objects themselves. They agree unless a runtime changes which cache
  class an architecture uses -- which is exactly the case worth catching, so a
  disagreement is a finding rather than a bug in this script.
* "Fits" compares one snapshot at the model's full context against the disk
  limit. A model that does not fit still reuses its cache within a session; it
  just cannot keep anything across a restart.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Workers"))

from common import cache_state  # noqa: E402


def find_models(root: Path) -> list[Path]:
    if (root / "config.json").is_file():
        return [root]
    return sorted({path.parent for path in root.rglob("config.json")})


def predicted_rollback(config: dict) -> str:
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    layer_types = text.get("layer_types")
    if isinstance(layer_types, (list, tuple)) and layer_types:
        if any("full" not in str(item).lower() for item in layer_types):
            return cache_state.ROLLBACK_CHECKPOINT
    elif text.get("full_attention_interval"):
        return cache_state.ROLLBACK_CHECKPOINT
    return cache_state.ROLLBACK_TRIM


def context_length(config: dict) -> int:
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    for key in ("max_position_embeddings", "model_max_length", "max_seq_len"):
        value = text.get(key) or config.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="a model directory, or a folder of them")
    parser.add_argument("--limit-gb", type=float, default=10.0,
                        help="the promptCache.diskMaxGB the report should judge against")
    parser.add_argument("--at-tokens", type=int, default=0,
                        help="judge at this prompt length instead of the model's full context")
    arguments = parser.parse_args()

    models = find_models(arguments.root)
    if not models:
        print(f"No config.json found under {arguments.root}", file=sys.stderr)
        return 1
    limit_bytes = int(arguments.limit_gb * (1 << 30))

    rows = []
    for directory in models:
        try:
            config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            rows.append((directory.name, "unreadable", str(exc)[:40], "", "", ""))
            continue
        budget = cache_state.model_cache_budget(config)
        rollback = predicted_rollback(config)
        if not budget["known"]:
            rows.append((directory.name, rollback, "unknown", "", "", ""))
            continue
        tokens = arguments.at_tokens or context_length(config)
        required = cache_state.snapshot_bytes(budget, tokens) if tokens else 0
        affordable = cache_state.affordable_tokens(budget, limit_bytes)
        fits = "yes" if required and required <= limit_bytes else "no"
        rows.append((
            directory.name,
            rollback,
            f"{budget['perTokenBytes'] / 1024:.1f} KB",
            f"{tokens:,}" if tokens else "?",
            f"{required / (1 << 30):.1f} GB" if required else "?",
            f"{fits} (holds {affordable:,})",
        ))

    headers = ("model", "rollback", "per token", "context", "snapshot", f"fits {arguments.limit_gb:g} GB")
    widths = [max(len(str(row[index])) for row in (*rows, headers)) for index in range(len(headers))]
    line = "  ".join(str(value).ljust(widths[index]) for index, value in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
