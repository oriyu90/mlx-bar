"""Optional real-model smoke test for the mlx-lm paged KV cache.

This intentionally uses one generated token and a private temporary cache root.
It never changes the user's MLXBar settings or existing prompt caches.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "Workers")

from mlx_lm_worker.adapter import MLXLMAdapter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="mlxbar-paged-model-"))
    os.environ.update({
        "MLXBAR_PROMPT_CACHE_DISK_ENABLED": "0",
        "MLXBAR_PROMPT_CACHE_MEMORY_RATIO": "0",
        "MLXBAR_PAGED_KV_ENABLED": "1",
        "MLXBAR_PAGED_KV_DISK_ENABLED": "1",
        "MLXBAR_PAGED_KV_BRANCH_REUSE": "1",
        "MLXBAR_PAGED_KV_MAX_BYTES": str(2 << 30),
        "MLXBAR_PAGED_KV_ROOT": str(root),
        "MLXBAR_MLX_MEMORY_LIMIT_BYTES": str(32 << 30),
    })
    adapter = MLXLMAdapter()
    try:
        capabilities = adapter.load(args.model)
        paged = capabilities.get("promptCache", {}).get("pagedKV", {})
        print("capability:", paged)
        if not paged.get("eligible"):
            print("RESULT: model is safely unsupported; no paged state was used")
            return 0
        # Stable ASCII text avoids relying on a model-specific chat template.
        prompt = ("This is an exact, stable prefix used only to verify local KV cache reuse. " * 220)
        adapter.prompt_cache.memory = None
        first = list(adapter.stream("paged-model-cold", {
            "prompt": prompt, "max_tokens": 1, "temperature": 0, "seed": 1234}))
        adapter.prompt_cache.memory = None
        second = list(adapter.stream("paged-model-warm", {
            "prompt": prompt, "max_tokens": 1, "temperature": 0, "seed": 1234}))
        first_metrics = next((item for item in reversed(first) if item.get("type") == "metrics"), {})
        second_metrics = next((item for item in reversed(second) if item.get("type") == "metrics"), {})
        stats = adapter.prompt_cache_stats().get("pagedKV", {})
        print("cold:", first_metrics)
        print("warm:", second_metrics)
        print("stats:", stats)
        passed = (second_metrics.get("cache_tier") == "paged_disk"
                  and int(second_metrics.get("cached_tokens", 0)) >= 256
                  and int(stats.get("tokensRestored", 0)) >= 256)
        print("RESULT:", "paged model smoke test passed" if passed else "FAILED")
        return 0 if passed else 1
    finally:
        adapter.unload()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
