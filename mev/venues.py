"""Base venues the executor is allowed to touch, and the ABI it speaks.

Every constant here was checked against mainnet with `eth_call` before being
written down (see docs/executor.md for the probe results). Nothing in this file
is a guess about an address.

Why pools and not routers: every venue Ares measured — Uniswap V3, Pancake V3,
Aerodrome Slipstream — implements the Uniswap V3 *pool* interface. Swapping the
pool directly removes a router address per venue, removes token approvals to
third-party contracts, and makes each hop's minimum output explicit.

Why Balancer and not Aave: Aave V3 charges 5bp on the borrowed notional. The
edges measured in Phase 0 are 3-20bp, so the loan fee would consume most of the
trade. Balancer V2's flash loan fee is zero.
"""
from __future__ import annotations

from eth_utils import keccak

CHAIN_ID = 8453

# --- assets -----------------------------------------------------------------

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"

TOKEN_DECIMALS = {USDC: 6, WETH: 18, CBBTC: 8}
TOKEN_SYMBOLS = {USDC: "USDC", WETH: "WETH", CBBTC: "cbBTC"}

# --- infrastructure ---------------------------------------------------------

# Balancer V2 Vault: `getProtocolFeesCollector()` returns 0xce8868...
# and `getFlashLoanFeePercentage()` is 0 on Base.
BALANCER_VAULT = "0xba12222222228d8ba445958a75a0704d566bf2c8"

# --- venues -----------------------------------------------------------------

# Pools whose swap() we have executed against real mainnet state in
# contracts/test/AresArbFork.t.sol. Anything not listed is refused at build
# time rather than risked: a pool with a different callback name would revert,
# and a pool with different semantics could mis-size a trade.
EXECUTABLE_DEX = frozenset({
    "uniswap-v3-base",
    "pancakeswap-v3-base",
    "aerodrome-slipstream",
    "aerodrome-slipstream-2",
    "aerodrome-slipstream-3",
    "aero-slipstream",
    "aero-slipstream-base",
})

# --- ABI --------------------------------------------------------------------

HOP_COMPONENTS = "(address,address,address,uint256,uint256,uint160,bool)"
EXECUTE_SIGNATURE = f"execute(address,uint256,{HOP_COMPONENTS}[],uint256,address)"
EXECUTE_SELECTOR = "0x" + keccak(text=EXECUTE_SIGNATURE)[:4].hex()


def is_executable(meta) -> tuple[bool, str]:
    """Whether a pool can be swapped directly by AresArb, and why not."""
    if meta.kind != "v3":
        return False, f"{meta.dex or 'pool'} is {meta.kind}, not a V3 pool"
    if meta.dex not in EXECUTABLE_DEX:
        return False, f"{meta.dex or 'unknown dex'} has not been verified"
    return True, ""


def loan_token_for(symbol: str) -> str:
    for address, name in TOKEN_SYMBOLS.items():
        if name == symbol:
            return address
    raise KeyError(symbol)
