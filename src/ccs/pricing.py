"""Model prices for the COST column.

Prices come from LiteLLM's public price table, the same source ccusage uses. The table is
downloaded once, trimmed to Claude models and cached for a week in the cache directory.
Set CCS_OFFLINE=1 to never download; without a cached table the COST column shows "—".
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional

PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
MAX_AGE_SECONDS = 7 * 24 * 3600

Prices = Dict[str, Dict[str, float]]


def load_prices(cache_dir: Path, download: bool = True) -> Prices:
    """Return {model: {input, output, cache_write, cache_write_1h, cache_read}} in USD per token,
    or {} if unavailable. download=False only reads the cached table, however old."""
    path = cache_dir / "prices-v3.json"
    fresh = path.exists() and time.time() - path.stat().st_mtime < MAX_AGE_SECONDS
    if download and not fresh and not os.environ.get("CCS_OFFLINE"):
        try:
            _download(path)
        except Exception:
            pass  # offline or blocked: fall back to a stale table, or to no prices at all
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _download(path: Path) -> None:
    import urllib.request  # here, not on top: it takes as long to import as the rest of ccs, and fzf runs ccs often

    with urllib.request.urlopen(PRICES_URL, timeout=10) as resp:
        table = json.load(resp)
    prices = {
        name: {
            "input": entry.get("input_cost_per_token") or 0.0,
            "output": entry.get("output_cost_per_token") or 0.0,
            "cache_write": entry.get("cache_creation_input_token_cost") or 0.0,
            # Claude Code writes its prompt cache with a 1-hour TTL, which is priced higher
            "cache_write_1h": entry.get("cache_creation_input_token_cost_above_1hr")
            or entry.get("cache_creation_input_token_cost")
            or 0.0,
            "cache_read": entry.get("cache_read_input_token_cost") or 0.0,
            "context": entry.get("max_input_tokens") or 0,  # the context window, in tokens
        }
        for name, entry in table.items()
        if name.startswith("claude-") and isinstance(entry, dict)
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prices))
    tmp.replace(path)


def context_window(model: str, prices: Prices, used: int) -> int:
    """The model's context window from the table, else the classic 200K. A session that already held more
    than that ran with the 1M window."""
    window = int((price_for(model, prices) or {}).get("context") or 200_000)
    return 1_000_000 if used > window else window


def price_for(model: str, prices: Prices) -> Optional[Dict[str, float]]:
    """Find prices for a model id such as 'claude-opus-5-5' or 'claude-sonnet-4-5-20250929'."""
    if model in prices:
        return prices[model]
    undated = re.sub(r"-\d{8}$", "", model)
    return prices.get(undated)
