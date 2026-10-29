"""Pool discovery and USD pricing.

GeckoTerminal gives us Base pools ranked by real 24h volume, which is a better
starting universe than a hand-written address list: it is the pools that
actually trade. Each address is then probed on-chain to decide whether it is
Uniswap-V3-style (slot0/liquidity) or V2-style (getReserves).

Discovery is cached for half an hour: the free GeckoTerminal tier rate-limits
at roughly 30 requests a minute, and a scan loop has no business refetching the
same list every twenty seconds.

Excluded on purpose: Uniswap V4 (singleton PoolManager, pool ids are not
contracts). Aerodrome/Velodrome are *included*: Slipstream pools expose the V3
interface and their fee() is read per pool. Their volatile pools expose V2
reserves, which is modelled as constant product.
"""
from __future__ import annotations

import json
import os
import time

import requests

from .chain import STABLES, WETH, PoolMeta, PoolState, Universe

GECKO = "https://api.geckoterminal.com/api/v2/networks/base/pools"
DISCOVERY_CACHE = "mev/cache/discovered.json"
DISCOVERY_TTL = 30 * 60

# Uniswap V4 pools are ids inside a singleton, not contracts.
EXCLUDED_DEX = {"uniswap-v4-base"}


def _read_cache(max_age: float) -> list[dict] | None:
    try:
        with open(DISCOVERY_CACHE, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if time.time() - blob.get("ts", 0) <= max_age:
            return blob.get("pools", [])
    except Exception:
        return None
    return None


def _write_cache(pools: list[dict]):
    os.makedirs(os.path.dirname(DISCOVERY_CACHE) or ".", exist_ok=True)
    with open(DISCOVERY_CACHE, "w", encoding="utf-8") as fh:
        json.dump({"ts": time.time(), "pools": pools}, fh)


def fetch_pools(pages: int = 3, sleep: float = 2.2, force: bool = False) -> list[dict]:
    """Top Base pools by 24h USD volume. Cached for DISCOVERY_TTL seconds."""
    if not force:
        cached = _read_cache(DISCOVERY_TTL)
        if cached:
            return cached

    found: dict[str, dict] = {}
    for page in range(1, pages + 1):
        for attempt in range(2):
            try:
                res = requests.get(
                    GECKO,
                    params={"page": page, "sort": "h24_volume_usd_desc"},
                    headers={"Accept": "application/json"},
                    timeout=20,
                )
                if res.status_code == 429:
                    time.sleep(10 * (attempt + 1))
                    continue
                res.raise_for_status()
                data = res.json().get("data", [])
                break
            except Exception:
                data = []
                time.sleep(3)
        for item in data:
            dex = (item.get("relationships", {}).get("dex", {}).get("data", {}) or {}).get("id", "")
            if dex in EXCLUDED_DEX:
                continue
            attrs = item.get("attributes", {})
            addr = (attrs.get("address") or "").lower()
            if not addr:
                continue
            try:
                volume = float((attrs.get("volume_usd") or {}).get("h24") or 0)
                tvl = float(attrs.get("total_reserve_in_usd") or 0)
            except (TypeError, ValueError):
                volume, tvl = 0.0, 0.0
            prev = found.get(addr)
            if prev and prev["volume"] >= volume:
                continue
            found[addr] = {
                "address": addr,
                "dex": dex,
                "name": attrs.get("name", ""),
                "volume": volume,
                "tvl": tvl,
            }
        time.sleep(sleep)

    pools = sorted(found.values(), key=lambda p: -p["volume"])
    if pools:
        _write_cache(pools)
    return pools


def load_pools(uni: Universe, top: int = 80, pages: int = 3,
               pools: list[dict] | None = None) -> list[tuple[PoolMeta, dict]]:
    discovered = (pools if pools is not None else fetch_pools(pages=pages))[:top]
    hints = {p["address"]: p["dex"] for p in discovered}
    metas = uni.resolve([p["address"] for p in discovered], hints)
    meta_by_addr = {m.address.lower(): m for m in metas}
    out: list[tuple[PoolMeta, dict]] = []
    for pool in discovered:
        meta = meta_by_addr.get(pool["address"])
        if meta:
            meta.dex = pool["dex"]
            out.append((meta, pool))
    uni.save()
    return out


def price_tokens(states: list[PoolState]) -> dict[str, float]:
    """USD price per token, anchored on stables and solved from the deepest pool.

    Two passes: stables and WETH first (the deep pairs), then everything that
    touches a priced token. A token with no path to a stable stays unpriced and
    its opportunities are dropped rather than guessed.
    """
    prices: dict[str, float] = {addr: 1.0 for addr in STABLES}
    for st in sorted(states, key=lambda s: -(s.reserve0 + s.reserve1)):
        for tok, other in ((st.meta.token0, st.meta.token1), (st.meta.token1, st.meta.token0)):
            if tok == WETH and other in STABLES:
                res_w, res_s = st.reserves_for(WETH)
                if res_w > 0:
                    price = res_s / res_w
                    if 100 < price < 20_000:
                        prices[WETH] = price
                        break
        if WETH in prices:
            break

    for _ in range(3):
        changed = False
        for st in sorted(states, key=lambda s: -(s.reserve0 + s.reserve1)):
            for tok, other in ((st.meta.token0, st.meta.token1), (st.meta.token1, st.meta.token0)):
                if tok in prices or other not in prices:
                    continue
                res_t, res_o = st.reserves_for(tok)
                if res_t > 0 and res_o > 0:
                    price = prices[other] * res_o / res_t
                    if price > 0:
                        prices[tok] = price
                        changed = True
        if not changed:
            break
    return prices


def eth_usd(states: list[PoolState]) -> float:
    """ETH price from the deepest WETH/stable pool, with a sane fallback."""
    prices = price_tokens(states)
    return prices.get(WETH, 0.0) or 3_000.0
