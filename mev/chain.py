"""Minimal JSON-RPC client for Base plus the pool reads the scanner needs.

Deliberately dependency-light: `requests` for transport and `eth_utils.keccak`
for selectors. No web3, no ABI codegen — the handful of calls we make are
encoded by hand and covered by tests.

Two failure modes matter here and both are handled explicitly:

* a call can *revert* (wrong interface for that pool) — that is information;
* a call can be *dropped* (public RPC under load) — that is not, and a pool
  must not be silently dropped from the universe because of it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Sequence

import requests
from eth_utils import keccak

BASE_RPC = "https://mainnet.base.org"
FALLBACK_RPCS = (
    "https://base-rpc.publicnode.com",
    "https://base-mainnet.public.blastapi.io",
)
GAS_ORACLE = "0x420000000000000000000000000000000000000F"
WETH = "0x4200000000000000000000000000000000000006"

# Base stables, treated as $1 for the scanner's price map.
STABLES = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDbC",
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": "DAI",
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": "USDT",
}

# Canonical decimals for the assets that matter. A dropped decimals() call
# defaulting USDC to 18 would misprice a pool by 10^12, so these never fall
# back to a guess.
KNOWN_DECIMALS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": 6,
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": 18,
    "0x4200000000000000000000000000000000000006": 18,
    "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf": 8,   # cbBTC
}


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


SEL = {
    "slot0": selector("slot0()"),
    "liquidity": selector("liquidity()"),
    "getReserves": selector("getReserves()"),
    "token0": selector("token0()"),
    "token1": selector("token1()"),
    "fee": selector("fee()"),
    "decimals": selector("decimals()"),
    "symbol": selector("symbol()"),
    "getL1Fee": selector("getL1Fee(bytes)"),
    "balanceOf": selector("balanceOf(address)"),
}


def pad_uint(value: int) -> str:
    return format(int(value), "x").rjust(64, "0")


def word(data: str, index: int) -> int:
    start = 2 + index * 64
    return int(data[start:start + 64] or "0", 16)


def decode_string(data: str) -> str:
    try:
        offset = word(data, 0)
        head = 2 + offset * 2
        length = int(data[head:head + 64], 16)
        if length > 128:
            return ""
        body = data[head + 64:head + 64 + length * 2]
        return bytes.fromhex(body).decode("utf-8", "replace").strip("\x00")
    except Exception:
        return ""


class RpcError(RuntimeError):
    pass


class Rpc:
    """JSON-RPC with batching, endpoint failover and a bounded retry budget."""

    def __init__(self, urls: str | Sequence[str] = BASE_RPC, timeout: float = 20.0,
                 retries: int = 1, min_interval: float = 0.35):
        self.urls = [urls] if isinstance(urls, str) else list(urls)
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval
        self.session = requests.Session()
        self.calls = 0
        self.failures = 0
        self.dropped = 0          # calls that never got an answer
        self._cursor = 0
        self._last_post = 0.0

    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last_post)
        if wait > 0:
            time.sleep(wait)
        self._last_post = time.time()

    def _post(self, payload):
        last = None
        order = self.urls[self._cursor:] + self.urls[:self._cursor]
        for url in order:
            for attempt in range(self.retries + 1):
                self._throttle()
                try:
                    res = self.session.post(url, json=payload, timeout=self.timeout)
                    if res.status_code == 429:
                        last = RpcError(f"429 from {url}")
                        time.sleep(2.5 * (attempt + 1))
                        continue
                    res.raise_for_status()
                    body = res.json()
                    self._cursor = self.urls.index(url)
                    return body
                except Exception as exc:  # noqa: BLE001 - failover, then surface
                    last = exc
                    time.sleep(0.4 * (attempt + 1))
        raise RpcError(f"all endpoints failed: {last}")

    def call(self, to: str, data: str, block: str = "latest"):
        return self.batch([(to, data)], block=block)[0]

    def batch(self, calls: Sequence[tuple[str, str]], block: str = "latest", chunk: int = 18):
        out: list[str | None] = []
        for start in range(0, len(calls), chunk):
            part = calls[start:start + chunk]
            payload = [
                {"jsonrpc": "2.0", "id": i, "method": "eth_call",
                 "params": [{"to": to, "data": data}, block]}
                for i, (to, data) in enumerate(part)
            ]
            try:
                answer = self._post(payload)
            except RpcError:
                self.calls += len(part)
                self.failures += len(part)
                self.dropped += len(part)
                out.extend([None] * len(part))
                continue
            by_id = {item.get("id"): item for item in answer} if isinstance(answer, list) else {}
            self.calls += len(part)
            for i in range(len(part)):
                item = by_id.get(i) or {}
                if "result" in item:
                    out.append(item["result"])
                else:
                    self.failures += 1
                    if "error" not in item:
                        self.dropped += 1
                    out.append(None)
        return out

    def call_raw(self, to: str, data: str, sender: str | None = None,
                 block: str = "latest") -> tuple[bool, str]:
        """eth_call that keeps the revert data instead of hiding it.

        A simulation that reverts is the answer, not a transport failure: the
        revert selector says whether the route lost money or was simply built
        wrong.
        """
        tx = {"to": to, "data": data}
        if sender:
            tx["from"] = sender
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [tx, block]}
        res = self._post(payload)
        if isinstance(res, dict) and "result" in res:
            return True, res["result"]
        err = (res.get("error") if isinstance(res, dict) else None) or {}
        return False, err.get("data") or err.get("message") or "unknown revert"

    def request(self, method: str, params: list):
        res = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if isinstance(res, dict) and "error" in res:
            raise RpcError(f"{method}: {res['error']}")
        return res.get("result") if isinstance(res, dict) else None

    def block_number(self) -> int:
        res = self._post({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        return int(res["result"], 16)

    def gas_price_wei(self) -> int:
        res = self._post({"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []})
        return int(res["result"], 16)

    def l1_fee_wei(self, size: int = 1200) -> int:
        """L1 data fee the OP Stack oracle charges for a tx of `size` bytes.

        The buffer is non-zero on purpose: an all-zero payload compresses to
        almost nothing and would understate the real fee by an order of
        magnitude.
        """
        body = bytes((i * 37 + 11) % 251 + 1 for i in range(size))
        padded = ((size + 31) // 32) * 32
        payload = body.hex().ljust(padded * 2, "0")
        data = SEL["getL1Fee"] + pad_uint(0x20) + pad_uint(size) + payload
        res = self.call(GAS_ORACLE, data)
        return word(res, 0) if res else 0


@dataclass
class PoolMeta:
    address: str
    token0: str
    token1: str
    dec0: int
    dec1: int
    kind: str          # "v3" | "v2"
    fee_ppm: int       # v3 units; v2 synthesised from fee_bp
    fee_bp: float
    dex: str = ""
    symbol0: str = ""
    symbol1: str = ""

    @property
    def pair(self) -> tuple[str, str]:
        return tuple(sorted((self.token0, self.token1)))

    @property
    def label(self) -> str:
        return f"{self.symbol0}/{self.symbol1}"


@dataclass
class PoolState:
    meta: PoolMeta
    reserve0: float
    reserve1: float
    liquidity: int = 0
    sqrt_price: float = 0.0
    note: str = ""

    def reserves_for(self, token_in: str) -> tuple[float, float]:
        if token_in == self.meta.token0:
            return self.reserve0, self.reserve1
        return self.reserve1, self.reserve0


class Universe:
    """Resolves pool metadata once, then reads cheap state on demand."""

    def __init__(self, rpc: Rpc, cache_path: str = "mev/cache/pools.json"):
        self.rpc = rpc
        self.cache_path = cache_path
        self.cache: dict[str, dict] = {}
        self.symbols: dict[str, str] = {}
        self.decimals: dict[str, int] = {}
        self._load()

    def _load(self):
        try:
            with open(self.cache_path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("version") == 2:
                self.cache = blob.get("pools", {})
                self.symbols = blob.get("symbols", {})
                self.decimals = blob.get("decimals", {})
        except Exception:
            self.cache = {}
        self.decimals.update(KNOWN_DECIMALS)

    def save(self):
        import os
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            json.dump({"version": 2, "pools": self.cache, "symbols": self.symbols,
                       "decimals": self.decimals}, fh)

    def resolve(self, addresses: Sequence[str], dex_hint: dict[str, str] | None = None) -> list[PoolMeta]:
        dex_hint = dex_hint or {}
        todo = [a.lower() for a in addresses if a.lower() not in self.cache]
        if todo:
            self._probe(todo, dex_hint)
        if self.cache:
            self._resolve_tokens({v["token0"] for v in self.cache.values()} |
                                 {v["token1"] for v in self.cache.values()})
        metas = []
        for addr in addresses:
            blob = self.cache.get(addr.lower())
            if not blob:
                continue
            blob["dec0"] = self.decimals.get(blob["token0"], blob["dec0"])
            blob["dec1"] = self.decimals.get(blob["token1"], blob["dec1"])
            blob["symbol0"] = self.symbols.get(blob["token0"], blob["symbol0"])
            blob["symbol1"] = self.symbols.get(blob["token1"], blob["symbol1"])
            metas.append(PoolMeta(**blob))
        return metas

    @staticmethod
    def _probe_calls(addr: str) -> list[tuple[str, str]]:
        return [(addr, SEL["token0"]), (addr, SEL["token1"]), (addr, SEL["slot0"]),
                (addr, SEL["liquidity"]), (addr, SEL["getReserves"]), (addr, SEL["fee"])]

    def _retry_row(self, addr: str, row: list) -> list:
        """Retry only the calls that never got an answer (a revert is final)."""
        if row[0] and row[1]:
            return row
        time.sleep(0.05)
        second = self.rpc.batch(self._probe_calls(addr), chunk=6)
        return [a or b for a, b in zip(row, second)]

    def _probe(self, addresses: list[str], dex_hint: dict[str, str]):
        calls = []
        for addr in addresses:
            calls.extend(self._probe_calls(addr))
        res = self.rpc.batch(calls, chunk=18)
        rows = [res[i * 6:(i + 1) * 6] for i in range(len(addresses))]
        for addr, raw in zip(addresses, rows):
            t0, t1, slot0, liq, reserves, fee = self._retry_row(addr, raw)
            if not t0 or not t1:
                continue
            token0 = "0x" + t0[-40:].lower()
            token1 = "0x" + t1[-40:].lower()
            if token0 == token1:
                continue
            if slot0 and liq:
                kind = "v3"
                fee_ppm = word(fee, 0) if fee else 3000
                fee_bp = fee_ppm / 100.0 if 0 < fee_ppm <= 100_000 else 30.0
            elif reserves:
                kind = "v2"
                fee_ppm = 3000
                fee_bp = 30.0
            else:
                continue
            dec0 = self.decimals.get(token0, 18)
            dec1 = self.decimals.get(token1, 18)
            self.cache[addr] = {
                "address": addr, "token0": token0, "token1": token1,
                "dec0": dec0, "dec1": dec1, "kind": kind, "fee_ppm": fee_ppm,
                "fee_bp": fee_bp, "dex": dex_hint.get(addr, ""),
                "symbol0": self.symbols.get(token0, token0[:8]),
                "symbol1": self.symbols.get(token1, token1[:8]),
            }
        self._resolve_tokens({v["token0"] for v in self.cache.values()} |
                             {v["token1"] for v in self.cache.values()})

    def _resolve_tokens(self, tokens: set[str]):
        missing = [t for t in tokens if t not in self.decimals or t not in self.symbols]
        if not missing:
            return
        calls = []
        for tok in missing:
            calls.extend([(tok, SEL["decimals"]), (tok, SEL["symbol"])])
        res = self.rpc.batch(calls, chunk=40)
        for i, tok in enumerate(missing):
            dec, sym = res[i * 2:(i + 1) * 2]
            self.decimals[tok] = KNOWN_DECIMALS.get(tok, word(dec, 0) if dec else 18)
            self.symbols[tok] = (decode_string(sym) if sym else "") or tok[:8]
        for blob in self.cache.values():
            blob["dec0"] = self.decimals.get(blob["token0"], 18)
            blob["dec1"] = self.decimals.get(blob["token1"], 18)
            blob["symbol0"] = self.symbols.get(blob["token0"], blob["token0"][:8])
            blob["symbol1"] = self.symbols.get(blob["token1"], blob["token1"][:8])

    def states(self, metas: Sequence[PoolMeta]) -> list[PoolState]:
        out, retry = self._read(metas)
        if retry:
            time.sleep(0.15)
            again, _ = self._read(retry)
            out.extend(again)
        return [s for s in out if s.reserve0 > 0 and s.reserve1 > 0]

    def _read(self, metas: Sequence[PoolMeta]) -> tuple[list[PoolState], list[PoolMeta]]:
        calls, plan = [], []
        for meta in metas:
            if meta.kind == "v3":
                calls.extend([(meta.address, SEL["slot0"]), (meta.address, SEL["liquidity"])])
                plan.append((meta, 2))
            else:
                calls.append((meta.address, SEL["getReserves"]))
                plan.append((meta, 1))
        res = self.rpc.batch(calls, chunk=18)
        out, retry, cursor = [], [], 0
        for meta, width in plan:
            chunk = res[cursor:cursor + width]
            cursor += width
            if any(item is None for item in chunk):
                retry.append(meta)
                continue
            try:
                out.append(self._parse(meta, chunk))
            except Exception:
                out.append(PoolState(meta, 0.0, 0.0, note="empty"))
        return [s for s in out if s.reserve0 > 0 and s.reserve1 > 0], retry

    @staticmethod
    def _parse(meta: PoolMeta, chunk: list) -> PoolState:
        if meta.kind == "v3":
            slot0, liq = chunk
            sqrt_x96 = word(slot0, 0) & ((1 << 160) - 1)
            L = word(liq, 0)
            if sqrt_x96 == 0 or L == 0:
                raise ValueError("empty pool")
            sp = sqrt_x96 / float(1 << 96)
            r0 = (L / sp) / 10 ** meta.dec0
            r1 = (L * sp) / 10 ** meta.dec1
            return PoolState(meta, r0, r1, liquidity=L, sqrt_price=sp)
        reserves = chunk[0]
        r0 = word(reserves, 0) / 10 ** meta.dec0
        r1 = word(reserves, 1) / 10 ** meta.dec1
        return PoolState(meta, r0, r1)
