"""Turn a measured dislocation into an executed trade on Base.

The scanner proves an edge exists. This module is the part that spends money to
capture it, and it is built around one rule: **a route that cannot be simulated
profitably is never submitted.**

Three modes:

    python -m mev.executor --demo            # full loop on a local Base fork
    python -m mev.executor --dry-run         # simulate the best live route
    python -m mev.executor --execute         # submit it (needs ARB_PRIVATE_KEY)

`--demo` starts anvil forked from Base mainnet, deploys the contract, scans real
pools, builds a real route, simulates it and executes it against real state.
Nothing leaves the machine and no key is involved — it is the whole code path
with a local fork in place of the chain.

`--execute` is the same path with a real key. It refuses to run unless the
simulation already shows a profit above the floor, so a losing trade can only
happen if the market moves between simulation and inclusion, and then the
contract's own check reverts it.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

import requests
from eth_abi import encode as abi_encode

from .chain import BASE_RPC, SEL, Rpc, Universe, pad_uint
from .math import amount_out
from .scanner import scan_once
from .universe import load_pools
from .venues import (BALANCER_VAULT, EXECUTE_SELECTOR, HOP_COMPONENTS, TOKEN_DECIMALS, USDC,
                     WETH, is_executable)

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"

ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

ERRORS = {
    "Unprofitable(uint256,uint256)": "route lost money after fees and slippage",
    "HopOutTooSmall(uint256,uint256,uint256)": "a pool paid less than the quoted minimum",
    "BadRoute()": "route failed validation",
    "NotOwner()": "executor owner is a different key",
    "TransferFailed()": "token transfer failed",
    "CallbackFromUnknownPool()": "a contract called the swap callback uninvited",
}
SELECTOR_NAMES = {}


def _selector(sig: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(text=sig)[:4].hex()


for _sig in ERRORS:
    SELECTOR_NAMES[_selector(_sig)] = _sig


# --------------------------------------------------------------------- routes

@dataclass(frozen=True)
class Hop:
    pool: str
    token_in: str
    token_out: str
    amount_in: int
    min_out: int
    price_limit: int = 0
    spend_balance: bool = False

    def as_tuple(self) -> tuple:
        return (self.pool, self.token_in, self.token_out, self.amount_in,
                self.min_out, self.price_limit, self.spend_balance)


@dataclass
class Route:
    loan_token: str
    loan_amount: int
    hops: list[Hop]
    expected_out: float          # human units of the loan token
    quoted_profit: float         # human units, before slippage haircut
    pair: str = ""
    buy_dex: str = ""
    sell_dex: str = ""
    edge_bp: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def expected_profit(self) -> float:
        return self.expected_out - self.loan_amount / 10 ** TOKEN_DECIMALS.get(self.loan_token, 18)

    def calldata(self, min_profit: int, profit_to: str) -> str:
        body = abi_encode(
            ["address", "uint256", f"{HOP_COMPONENTS}[]", "uint256", "address"],
            [self.loan_token, self.loan_amount, [h.as_tuple() for h in self.hops],
             min_profit, profit_to],
        )
        return EXECUTE_SELECTOR + body.hex()


def _oriented(reserves: list[float], meta, token_in: str) -> tuple[float, float]:
    if token_in == meta.token0:
        return reserves[0], reserves[1]
    return reserves[1], reserves[0]


def _decimals(token: str, defaults: dict[str, int]) -> int:
    return TOKEN_DECIMALS.get(token) or defaults.get(token) or 18


def build_route(opp: dict, defaults: dict[str, int], slippage_bps: float = 50.0,
                min_profit_usd: float = 0.30, gas_usd: float = 0.0) -> tuple[Route | None, str]:
    """Turn one scanner opportunity into executor calldata, or explain why not.

    Sizing comes from the scanner's closed form. Each hop's floor sits
    `slippage_bps` below its quote, so a fill that degrades beyond that reverts
    on chain instead of completing at a worse price. The floors are not used to
    re-quote the route: an expectation built from worst-case fills would
    understate every edge and refuse trades that are fine.
    """
    buy, sell = opp["buy"], opp["sell"]
    ok, why = is_executable(buy)
    if not ok:
        return None, f"buy side: {why}"
    ok, why = is_executable(sell)
    if not ok:
        return None, f"sell side: {why}"

    token_in = opp["token_in"]
    token_out = sell.token0 if token_in == sell.token1 else sell.token1
    dec_in = _decimals(token_in, defaults)
    dec_out = _decimals(token_out, defaults)

    slip = max(0.0, min(slippage_bps, 500.0)) / 10_000.0
    q = opp["sizing"].q
    if q <= 0:
        return None, "sizing produced zero input"

    a_in, a_out = _oriented(opp["buy_reserves"], buy, token_in)
    b_in, b_out = _oriented(opp["sell_reserves"], sell, token_out)

    quoted1 = amount_out(q, a_in, a_out, buy.fee_bp)
    # Hop two spends whatever hop one delivered; quote it from that amount.
    quoted2 = amount_out(quoted1, b_in, b_out, sell.fee_bp)
    if quoted2 <= q:
        return None, "route is not profitable at the quoted depth"

    amount_in = int(q * 10 ** dec_in)
    min_out1 = int(quoted1 * (1 - slip) * 10 ** dec_out)
    min_out2 = int(quoted2 * (1 - slip) * 10 ** dec_in)
    if min_out1 <= 0 or min_out2 <= 0:
        return None, "minimum output rounded to zero"

    notes = []
    if min_profit_usd > 0 and opp.get("usd_in"):
        floor = min_profit_usd + 2 * gas_usd
        notes.append(f"floor ${floor:.2f}")

    route = Route(
        loan_token=token_in,
        loan_amount=amount_in,
        hops=[
            Hop(pool=buy.address, token_in=token_in, token_out=token_out,
                amount_in=amount_in, min_out=min_out1),
            Hop(pool=sell.address, token_in=token_out, token_out=token_in,
                amount_in=0, min_out=min_out2, spend_balance=True),
        ],
        expected_out=quoted2,
        quoted_profit=quoted2 - q,
        pair=f"{buy.symbol0}/{buy.symbol1}",
        buy_dex=buy.dex,
        sell_dex=sell.dex,
        edge_bp=opp["sizing"].edge_bp,
        notes=notes,
    )
    return route, ""


def min_profit_raw(route: Route, min_profit_usd: float, gas_usd: float,
                   price_usd: float) -> int:
    dec = _decimals(route.loan_token, {})
    if min_profit_usd <= 0 and gas_usd <= 0:
        return 0
    if price_usd <= 0:
        return 0
    floor = max(min_profit_usd, gas_usd)       # cover gas before counting profit
    return int(floor / price_usd * 10 ** dec)


# ---------------------------------------------------------------- simulation

@dataclass
class Simulation:
    ok: bool
    profit_raw: int = 0
    reason: str = ""


def simulate(rpc: Rpc, sender: str, executor: str, data: str) -> Simulation:
    ok, raw = rpc.call_raw(executor, data, sender=sender)
    if ok:
        try:
            return Simulation(True, int(raw, 16))
        except ValueError:
            return Simulation(True, 0)
    reason = _decode_error(raw)
    return Simulation(False, 0, reason)


def _decode_error(raw: str) -> str:
    if not raw or not raw.startswith("0x") or len(raw) < 10:
        return str(raw)[:160]
    sel = raw[:10]
    if sel == "0x08c379a0":  # Error(string), usually from the Vault or a token
        try:
            from eth_abi import decode as abi_decode
            return f"revert: {abi_decode(['string'], bytes.fromhex(raw[10:]))[0]}"
        except Exception:
            return "revert: Error(string) that would not decode"
    name = SELECTOR_NAMES.get(sel)
    if not name:
        return f"revert {sel}"
    return ERRORS.get(name, name)


# Fractions of the scanner's size to try, largest first. The closed form is a
# good hint but it is a constant-product approximation of concentrated
# liquidity; the chain, not the model, decides what actually fills.
SIZE_STEPS = (1.0, 0.6, 0.35, 0.2, 0.1, 0.05, 0.02, 0.01)


def _sized(opp: dict, q: float) -> dict:
    from types import SimpleNamespace
    clone = dict(opp)
    clone["sizing"] = SimpleNamespace(q=q, edge_bp=opp["sizing"].edge_bp,
                                      impact=opp["sizing"].impact)
    return clone


def plan_route(rpc: Rpc, sender: str, executor: str, opp: dict, decimals: dict,
               slippage_bps: float, min_profit_usd: float, gas_usd: float,
               profit_to: str) -> tuple[tuple[Route, str, Simulation] | None, str]:
    """Find a size of this opportunity that actually simulates as profitable.

    Returns (route, calldata, simulation) or (None, reason). Trying several
    sizes costs a few `eth_call`s and removes the single biggest source of
    false positives: supersized trades that the model believes and the pools
    do not.
    """
    reason = "no size simulated profitably"
    for factor in SIZE_STEPS:
        q = opp["sizing"].q * factor
        if q <= 0:
            continue
        route, why = build_route(_sized(opp, q), decimals, slippage_bps,
                                 min_profit_usd, gas_usd)
        if not route:
            return None, why
        lends, why = vault_can_lend(rpc, route.loan_token, route.loan_amount)
        if not lends:
            reason = why
            continue          # a smaller size may fit what the Vault holds
        floor = min_profit_raw(route, min_profit_usd, gas_usd, opp["usd_in"])
        data = route.calldata(floor, profit_to)
        sim = simulate(rpc, sender, executor, data)
        if sim.ok and sim.profit_raw > 0:
            return (route, data, sim), ""
        reason = sim.reason or "simulation showed no profit"
        if not sim.ok and "lost money" not in reason and "less than the quoted" not in reason:
            return None, reason      # a structural problem; smaller won't help
    return None, reason


def erc20_balance(rpc: Rpc, token: str, holder: str) -> int:
    res = rpc.call(token, SEL["balanceOf"] + pad_uint(int(holder, 16)))
    return int(res, 16) if res else 0


def vault_can_lend(rpc: Rpc, token: str, amount: int, share: float = 0.5) -> tuple[bool, str]:
    """The Vault can only lend what it holds; borrowing more reverts."""
    available = erc20_balance(rpc, token, BALANCER_VAULT)
    if available < amount:
        dec = TOKEN_DECIMALS.get(token, 18)
        return False, (f"vault holds {available / 10 ** dec:,.2f} of the loan token, "
                       f"route needs {amount / 10 ** dec:,.2f}")
    if amount > available * share:
        return False, "route would borrow more than half of the vault's balance"
    return True, ""


# ------------------------------------------------------------------ execution

def _receipt(rpc: Rpc, tx_hash: str, timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = rpc.request("eth_getTransactionReceipt", [tx_hash])
        if res:
            return res
        time.sleep(0.25)
    raise RuntimeError(f"no receipt for {tx_hash} after {timeout:.0f}s")


def send(rpc: Rpc, sender: str, executor: str, data: str,
         private_key: str | None = None) -> dict:
    """Submit the route. Unlocked sender for a local fork, signed for mainnet."""
    if private_key:
        from eth_account import Account
        account = Account.from_key(private_key)
        if account.address.lower() != sender.lower():
            raise RuntimeError("ARB_PRIVATE_KEY does not match --sender")
        tx = {
            "to": executor,
            "data": data,
            "value": 0,
            "nonce": int(rpc.request("eth_getTransactionCount", [sender, "pending"]), 16),
            "chainId": int(rpc.request("eth_chainId", []), 16),
            "gasPrice": int(rpc.request("eth_gasPrice", []), 16),
        }
        estimate = rpc.request("eth_estimateGas", [{"from": sender, "to": executor, "data": data}])
        tx["gas"] = max(int(int(estimate, 16) * 1.5), 1_200_000)
        signed = Account.sign_transaction(tx, private_key)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = rpc.request("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
    else:
        # A fixed ceiling: anvil's estimate is tight, and a swap that crosses
        # ticks can cost noticeably more than the estimate on the first pass.
        tx_hash = rpc.request("eth_sendTransaction",
                              [{"from": sender, "to": executor, "data": data,
                                "value": "0x0", "gas": hex(2_500_000)}])
    return _receipt(rpc, tx_hash)


# ----------------------------------------------------------------------- demo

class AnvilFork:
    """A throwaway Base fork on a free port."""

    def __init__(self, url: str, block: int | None = None):
        binary = shutil.which("anvil") or str(pathlib.Path.home() / ".foundry" / "bin" / "anvil.exe")
        self.port = _free_port()
        # Anvil defaults to a ~2.3 gwei gas price, which is two orders of
        # magnitude above Base's real ~0.03 gwei and would make every trade
        # look unprofitable. Pin it to the measured mainnet level.
        cmd = [binary, "--fork-url", url, "--port", str(self.port), "--chain-id", "8453",
               "--gas-price", "30000000", "--silent"]
        if block:
            cmd += ["--fork-block-number", str(block)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode("utf-8", "replace")[-400:]
                raise RuntimeError(f"anvil exited: {err}")
            try:
                res = requests.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "eth_blockNumber", "params": []},
                                    timeout=2)
                if res.status_code == 200 and "result" in res.json():
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.4)
        self.__exit__(None, None, None)
        raise RuntimeError("anvil did not become ready in 60s")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def deploy(url: str, owner: str, private_key: str) -> str:
    forge = shutil.which("forge") or str(pathlib.Path.home() / ".foundry" / "bin" / "forge.exe")
    cmd = [forge, "create", "src/AresArb.sol:AresArb", "--rpc-url", url,
           "--private-key", private_key, "--broadcast", "--json",
           "--constructor-args", owner, BALANCER_VAULT]
    proc = subprocess.run(cmd, cwd=CONTRACTS, capture_output=True, text=True, timeout=180)
    # `forge create --json` prints its receipt to stderr on some versions and
    # stdout on others; the receipt is the only line we need either way.
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    found = re.search(r'"deployedTo"\s*:\s*"(0x[0-9a-fA-F]{40})"', blob)
    if found:
        return found.group(1)
    raise RuntimeError(f"deploy failed: {blob[-400:]}")


def inject_price_move(rpc: Rpc, pool: str, bps: float) -> None:
    """Move one pool's sqrtPrice on a fork so there is a real dislocation.

    This is a harness, not a strategy: the pools, the vault, the tokens and the
    executor are all real, but the gap is ours. It exists so the capture path
    can be proven end to end at a size worth measuring, instead of waiting for
    a market dislocation that may not arrive while the demo runs.
    """
    raw = rpc.call(pool, SEL["slot0"])
    if not raw or len(raw) < 2 + 7 * 64:
        raise RuntimeError(f"cannot read slot0 of {pool}")
    # slot0() returns the struct unpacked as seven words; storage is the packed
    # form. Rebuilding it is not optional: writing a bare sqrtPrice zeroes the
    # `unlocked` flag and every swap then reverts with LOK.
    words = [int(raw[2 + i * 64:2 + (i + 1) * 64], 16) for i in range(7)]
    sqrt, tick, obs_index, obs_card, obs_card_next, fee_proto, unlocked = words
    new_sqrt = int(sqrt * (1 + bps / 10_000.0) ** 0.5)
    packed = (new_sqrt
              | (tick & 0xFFFFFF) << 160
              | (obs_index & 0xFFFF) << 184
              | (obs_card & 0xFFFF) << 200
              | (obs_card_next & 0xFFFF) << 216
              | (fee_proto & 0xFF) << 232
              | (unlocked & 0xFF) << 240)
    rpc.request("anvil_setStorageAt", [pool, "0x" + "0" * 64,
                                      "0x" + format(packed, "064x")])


def _print_route(route: Route, block: int, gas_usd: float) -> None:
    print(f"  block {block:,}   {route.pair}   edge {route.edge_bp:.1f}bp")
    print(f"  {'':2s}buy   {route.buy_dex:22s} {route.hops[0].pool}")
    print(f"  {'':2s}sell  {route.sell_dex:22s} {route.hops[1].pool}")
    dec = TOKEN_DECIMALS.get(route.loan_token, 18)
    print(f"  {'':2s}size  {route.loan_amount / 10 ** dec:,.2f} "
          f"{'USDC' if dec == 6 else 'units'}   quoted profit "
          f"{route.quoted_profit:,.4f}   gas ${gas_usd:.4f}")


def run_demo(args) -> int:
    url = args.rpc or BASE_RPC
    print("=" * 78)
    print("Ares executor — full loop on a local Base fork")
    print("Real pools, real Balancer flash loan, real contract. No key, no funds.")
    print("=" * 78)

    rpc_probe = Rpc(url, min_interval=0.0)
    tip = rpc_probe.block_number()
    attempts = max(1, args.demo_blocks)
    reasons: list[str] = []
    best = None

    for offset in range(attempts):
        block = args.fork_block if args.fork_block else tip - offset * 5
        print(f"\n[attempt {offset + 1}/{attempts}] forking Base at block {block:,}")
        try:
            with AnvilFork(url, block) as node:
                rpc = Rpc(node.url, min_interval=0.0, retries=2)
                owner = ANVIL_ADDRESS
                executor = deploy(node.url, owner, ANVIL_KEY)
                print(f"  deployed AresArb at {executor}")

                uni = Universe(rpc)
                if args.inject_bps:
                    states = uni.states([m for m, _ in load_pools(uni, top=args.top,
                                                                 pages=args.pages)])
                    weth_usdc = [s for s in states
                                 if {s.meta.token0, s.meta.token1} == {USDC, WETH}]
                    if not weth_usdc:
                        raise RuntimeError("no WETH/USDC pool to perturb")
                    target = max(weth_usdc, key=lambda s: s.reserve0 * s.reserve1)
                    inject_price_move(rpc, target.meta.address, args.inject_bps)
                    print(f"  harness: moved {target.meta.address[:10]} by "
                          f"{args.inject_bps:+.0f}bp to create a known dislocation")
                scan = scan_once(rpc, uni, args.top, args.pages, 0.0, None, quiet=True,
                                 gas_price_wei=args.fork_gas_price_wei)
                live = [r for r in scan["results"] if r["net_usd"] > 0]
                print(f"  {scan['states']} live pools, {len(live)} profitable routes, "
                      f"gas ${scan['gas_usd']:.4f}/attempt")
                if not live:
                    near = scan["results"][0] if scan["results"] else None
                    msg = ("no profitable route at this block"
                           + (f" (best near-miss {near['sizing'].edge_bp:.1f}bp)"
                              if near else ""))
                    print(f"  {msg}")
                    reasons.append(msg)
                    continue

                for opp in live[:args.candidates]:
                    print()
                    print(f"  {opp['pair'].replace('->', ' / ')}  "
                          f"{opp['buy'].dex} -> {opp['sell'].dex}  "
                          f"edge {opp['sizing'].edge_bp:.1f}bp  "
                          f"model ${opp['net_usd']:.2f} net")
                    planned, why = plan_route(rpc, owner, executor, opp, uni.decimals,
                                              args.slippage_bps, args.min_profit_usd,
                                              scan["gas_usd"], args.profit_to or owner)
                    if not planned:
                        print(f"  refused: {why}")
                        reasons.append(why)
                        continue
                    route, data, sim = planned
                    floor = min_profit_raw(route, args.min_profit_usd, scan["gas_usd"],
                                           opp["usd_in"])
                    dec = TOKEN_DECIMALS.get(route.loan_token, 18)
                    print(f"  chosen size {route.loan_amount / 10 ** dec:,.2f} "
                          f"({'USDC' if dec == 6 else 'units'}); floor "
                          f"{floor / 10 ** dec:,.4f}")
                    _print_route(route, scan["block"], scan["gas_usd"])
                    print(f"  simulation: profit {sim.profit_raw / 10 ** dec:,.6f} "
                          f"({'USDC' if dec == 6 else 'units'})")

                    before = erc20_balance(rpc, route.loan_token, args.profit_to or owner)
                    receipt = send(rpc, owner, executor, data)
                    after = erc20_balance(rpc, route.loan_token, args.profit_to or owner)
                    gas_used = int(receipt.get("gasUsed", "0x0"), 16)
                    gas_cost = gas_used * int(receipt.get("effectiveGasPrice", "0x0"), 16) / 1e18
                    print(f"  EXECUTED  tx {receipt['transactionHash']}  status "
                          f"{int(receipt['status'], 16)}  gas {gas_used:,}")
                    print(f"  treasury received {((after - before) / 10 ** dec):,.6f}  "
                          f"gas cost {gas_cost:.8f} ETH")
                    print(f"\n  RESULT: one atomic round trip executed and settled "
                          f"on forked Base state.")
                    print("  This is the exact code path --execute uses against mainnet.")
                    return 0

        except Exception as exc:  # noqa: BLE001 - a demo should report, not crash
            msg = f"{type(exc).__name__}: {exc}"
            print(f"  attempt failed: {msg}")
            reasons.append(msg)

    print("\n" + "-" * 78)
    print("No executable route was found on these blocks.")
    for why in reasons[-6:]:
        print(f"  · {why}")
    print("That is a real measurement, not a failure: the scanner only reports an")
    print("edge when one exists at that block. Re-run in a few minutes.")
    return 1


# ------------------------------------------------------------- live execution

def _load_env() -> None:
    """Read .env without importing the dashboard's settings module."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run_live(args) -> int:
    _load_env()
    url = args.rpc or os.environ.get("BASE_RPC", BASE_RPC)
    executor = args.executor or os.environ.get("ARB_EXECUTOR", "")
    key = os.environ.get("ARB_PRIVATE_KEY", "")
    owner = ""
    if key:
        from eth_account import Account
        owner = Account.from_key(key).address
    owner = owner or args.sender or os.environ.get("ARB_OWNER", "")
    profit_to = args.profit_to or os.environ.get("ARB_PROFIT_ADDRESS", "") or owner
    if not executor:
        print("No executor address. Deploy one first:\n"
              "  python -m mev.executor --deploy --rpc <url>\n"
              "or run the whole loop locally:\n"
              "  python -m mev.executor --demo", file=sys.stderr)
        return 2
    if not owner:
        print("Set ARB_PRIVATE_KEY (preferred) or ARB_OWNER to the executor's owner address.",
              file=sys.stderr)
        return 2
    if args.execute and not key:
        print("--execute needs ARB_PRIVATE_KEY in the environment or .env.", file=sys.stderr)
        return 2
    rpc = Rpc(url)
    sender = owner

    uni = Universe(rpc)
    scan = scan_once(rpc, uni, args.top, args.pages, 0.0, os.path.join("mev/out", "opportunities.jsonl"),
                     quiet=True)
    live = [r for r in scan["results"] if r["net_usd"] > 0]
    print(f"block {scan['block']:,} · {scan['states']} pools · {len(live)} routes above zero · "
          f"gas ${scan['gas_usd']:.4f}")
    if not live:
        print("no dislocation right now — the desk only trades when one exists")
        return 1

    for opp in live[:args.candidates]:
        planned, why = plan_route(rpc, sender, executor, opp, uni.decimals, args.slippage_bps,
                                  args.min_profit_usd, scan["gas_usd"], profit_to)
        if not planned:
            print(f"skipped: {why}")
            continue
        route, data, sim = planned
        _print_route(route, scan["block"], scan["gas_usd"])
        print(f"  simulation: profit {sim.profit_raw / 10 ** TOKEN_DECIMALS.get(route.loan_token, 18):,.6f}")
        if not args.execute:
            print("  dry run only (pass --execute with ARB_PRIVATE_KEY to send)")
            return 0
        receipt = send(rpc, sender, executor, data, private_key=key)
        print(f"  SENT tx {receipt['transactionHash']} status {int(receipt['status'], 16)} "
              f"gas {int(receipt['gasUsed'], 16):,}")
        return 0 if int(receipt["status"], 16) == 1 else 1

    print("every candidate was refused by simulation — nothing submitted")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ares Base arbitrage executor")
    ap.add_argument("--demo", action="store_true", help="run the whole loop on a local fork")
    ap.add_argument("--dry-run", action="store_true", help="simulate the best live route")
    ap.add_argument("--execute", action="store_true", help="submit the best live route")
    ap.add_argument("--deploy", action="store_true", help="deploy the executor contract")
    ap.add_argument("--rpc", default="")
    ap.add_argument("--executor", default="")
    ap.add_argument("--profit-to", default="")
    ap.add_argument("--sender", default="", help="executor owner address for simulations")
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--candidates", type=int, default=8)
    ap.add_argument("--slippage-bps", type=float, default=50.0)
    ap.add_argument("--min-profit-usd", type=float, default=0.30)
    ap.add_argument("--demo-blocks", type=int, default=4)
    ap.add_argument("--fork-block", type=int, default=0,
                    help="pin the demo to one historical block")
    ap.add_argument("--inject-bps", type=float, default=0.0,
                    help="harness: move one pool's price to create a test dislocation")
    ap.add_argument("--fork-gas-price-wei", type=int, default=30_000_000,
                    help="Base's measured ~0.03 gwei, since anvil reports 2.3 gwei")
    args = ap.parse_args(argv)

    if args.deploy:
        _load_env()
        key = os.environ.get("ARB_PRIVATE_KEY", "")
        if not key:
            print("--deploy needs ARB_PRIVATE_KEY (the owner key that will call execute).",
                  file=sys.stderr)
            return 2
        from eth_account import Account
        owner = Account.from_key(key).address
        address = deploy(args.rpc or os.environ.get("BASE_RPC", BASE_RPC), owner, key)
        print(f"deployed AresArb at {address}\nadd to .env:\nARB_EXECUTOR={address}")
        return 0
    if args.demo:
        return run_demo(args)
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
