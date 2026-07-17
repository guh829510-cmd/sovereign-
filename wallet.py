"""
wallet.py — the agent's money.

A focused, defensive wrapper around a **Coinbase CDP non-custodial wallet**
(``coinbase-agentkit`` 0.2.x ``CdpWalletProvider``), locked to the Base Sepolia
testnet.

How this actually works (verified against the installed 0.2.0 API)
------------------------------------------------------------------
* **Key generation & custody.** ``CdpWalletProvider`` wraps a CDP
  *developer-managed* wallet: on first run ``Wallet.create()`` generates a fresh
  address and a **client-side seed** (the private key material) held in this
  process — non-custodial in the truest sense. We authenticate to CDP's APIs
  with a separate API key (``CDP_API_KEY_NAME`` / ``CDP_API_KEY_PRIVATE_KEY``);
  the seed is what actually signs.

* **Persistence = the seed.** To let a restarted or *cloned* agent re-attach to
  the same funds instead of orphaning them, we export the wallet
  (``export_wallet()`` → seed + wallet id) and write it to a local file. That
  file is a **secret**: it is created with ``0600`` permissions and is
  git-ignored. Losing it means losing the money; leaking it means someone else
  can spend it.

* **Balance errors must never look like "broke".** The survival loop terminates
  the server when the balance drops below $1. If an RPC hiccup returned 0, the
  agent would kill itself over a network blip. Every read retries with backoff
  and, on exhaustion, raises :class:`BalanceUnavailable` — a signal the caller
  must treat as "unknown, try again later", *never* as zero.

Env / config (put these in a local ``.env`` — never commit it):
    CDP_API_KEY_NAME, CDP_API_KEY_PRIVATE_KEY   (required)
    NETWORK_ID              optional — defaults to base-sepolia (and is enforced)
    WALLET_DATA_FILE        optional — path for the persisted (secret) seed
    ETH_USD_PRICE           optional — nominal price for USD threshold math
    GAS_RESERVE_ETH         optional — ETH held back to cover gas
"""

from __future__ import annotations

import json
import logging
import os
import stat
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from coinbase_agentkit import CdpWalletProvider, CdpWalletProviderConfig

try:  # web3 is a hard dep; fail with a clear message if the env is broken
    from web3 import Web3
except ImportError as exc:  # pragma: no cover
    raise ImportError("wallet.py requires web3 — see requirements.txt") from exc

load_dotenv()
logger = logging.getLogger(__name__)

# --- constants -------------------------------------------------------------
NETWORK_ID = "base-sepolia"
WEI_PER_ETH = Decimal(10) ** 18
WALLET_DATA_FILE = Path(os.getenv("WALLET_DATA_FILE", "wallet_data.json"))

# Testnet ETH has NO market price. This nominal figure exists only so the
# USD-denominated survival thresholds in main.py are exercisable in simulation.
# On mainnet, replace usd_balance() with a real oracle read (e.g. Pyth).
ETH_USD_PRICE = Decimal(os.getenv("ETH_USD_PRICE", "3000"))

# Keep a little ETH back so a transfer can't leave the wallet unable to pay gas.
GAS_RESERVE_ETH = Decimal(os.getenv("GAS_RESERVE_ETH", "0.00002"))

# Transient failures we retry. Kept broad on purpose: connectivity to an RPC or
# to CDP fails in many shapes (httpx transport errors, DNS, resets, timeouts).
try:  # httpx ships with the cdp-sdk, but don't hard-fail if it moved
    import httpx

    _TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
        httpx.TimeoutException,
        httpx.TransportError,
        ConnectionError,
        TimeoutError,
        OSError,
    )
except ImportError:  # pragma: no cover
    _TRANSIENT_ERRORS = (ConnectionError, TimeoutError, OSError)


# --- errors ----------------------------------------------------------------
class WalletError(Exception):
    """Base class for all wallet failures."""


class WalletConfigError(WalletError):
    """Missing/invalid credentials or configuration. Not retryable."""


class BalanceUnavailable(WalletError):
    """Balance could not be read after retries. MUST NOT be treated as zero."""


class TransferError(WalletError):
    """A transfer could not be completed (validation or on-chain failure)."""


# A single retry policy reused across every network call: 4 attempts with
# 2s -> 4s -> 8s -> 16s backoff, re-raising the underlying error on give-up.
_network_retry = retry(
    retry=retry_if_exception_type(_TRANSIENT_ERRORS),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=16),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class Wallet:
    """A Base Sepolia CDP wallet with graceful, survival-safe operations."""

    def __init__(
        self,
        wallet_data: str | None = None,
        *,
        use_persisted: bool = True,
        persist_new: bool = True,
    ) -> None:
        """Load or create a CDP wallet.

        Default behaviour (no args): reuse the persisted seed if present,
        otherwise create a fresh wallet and persist it. Pass
        ``use_persisted=False`` to force a brand-new wallet (see
        :meth:`new_child`), and ``persist_new=False`` to avoid overwriting the
        primary seed file with it.
        """
        api_key_name, api_key_private_key = self._require_credentials()

        if wallet_data is None and use_persisted:
            wallet_data = self._load_persisted_wallet_data()
        is_new = wallet_data is None

        try:
            self._provider = self._build_provider(
                api_key_name=api_key_name,
                api_key_private_key=api_key_private_key,
                wallet_data=wallet_data,
            )
        except _TRANSIENT_ERRORS as exc:  # network never recovered
            raise WalletError(f"Could not reach CDP to init wallet: {exc}") from exc
        except WalletError:
            raise
        except Exception as exc:  # bad key, corrupt seed, etc. — not retryable
            raise WalletConfigError(f"CDP wallet init failed: {exc}") from exc

        self.address = self._provider.get_address()

        network_id = getattr(self._provider.get_network(), "network_id", None)
        if network_id != NETWORK_ID:
            raise WalletConfigError(
                f"Refusing to run: wallet is on {network_id!r}, expected {NETWORK_ID!r}."
            )

        if is_new and persist_new:
            self._persist_wallet_data()
            logger.info("Created new CDP wallet on %s: %s", NETWORK_ID, self.address)
        elif is_new:
            logger.info("Created ephemeral (unpersisted) CDP wallet: %s", self.address)
        else:
            logger.info("Loaded existing CDP wallet on %s: %s", NETWORK_ID, self.address)

    @classmethod
    def new_child(cls) -> "Wallet":
        """Create a fresh child wallet (new seed) without touching the parent's
        persisted seed file. Used by the reproduction flow."""
        return cls(use_persisted=False, persist_new=False)

    def export_wallet_data(self) -> str:
        """Serialise this wallet's seed (JSON). SECRET — handle with care."""
        return json.dumps(self._provider.export_wallet().to_dict())

    # -- construction helpers ------------------------------------------------
    @staticmethod
    def _require_credentials() -> tuple[str, str]:
        name = os.getenv("CDP_API_KEY_NAME")
        private_key = os.getenv("CDP_API_KEY_PRIVATE_KEY")
        missing = [
            var
            for var, val in (
                ("CDP_API_KEY_NAME", name),
                ("CDP_API_KEY_PRIVATE_KEY", private_key),
            )
            if not val
        ]
        if missing:
            raise WalletConfigError("Missing required CDP credentials: " + ", ".join(missing))
        return name, private_key

    @staticmethod
    @_network_retry
    def _build_provider(
        *,
        api_key_name: str,
        api_key_private_key: str,
        wallet_data: str | None,
    ) -> CdpWalletProvider:
        """Create (or re-import) the CDP wallet provider (does network I/O)."""
        return CdpWalletProvider(
            CdpWalletProviderConfig(
                api_key_name=api_key_name,
                api_key_private_key=api_key_private_key,
                network_id=NETWORK_ID,
                wallet_data=wallet_data,  # None -> Wallet.create() a fresh seed
            )
        )

    # -- persistence (secret!) ----------------------------------------------
    @staticmethod
    def _load_persisted_wallet_data() -> str | None:
        """Return the persisted wallet_data JSON string, or None for a fresh wallet."""
        if not WALLET_DATA_FILE.exists():
            return None
        try:
            raw = WALLET_DATA_FILE.read_text()
            json.loads(raw)  # validate it parses before handing to the SDK
            return raw
        except (json.JSONDecodeError, OSError) as exc:
            # Refuse to silently create a *new* wallet when a seed file exists
            # but is unreadable — that would strand the old wallet's funds.
            raise WalletConfigError(
                f"Wallet seed file {WALLET_DATA_FILE} exists but is unreadable: {exc}"
            ) from exc

    def _persist_wallet_data(self) -> None:
        """Export the seed and write it to a 0600, git-ignored file."""
        try:
            data = self._provider.export_wallet().to_dict()
            # Create the file with restrictive perms *before* writing secrets.
            fd = os.open(
                WALLET_DATA_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
            )
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
        except OSError as exc:
            # Fatal on first creation: a wallet whose seed we can't save is a
            # wallet we will lose access to on the next restart.
            raise WalletConfigError(
                f"Failed to persist wallet seed to {WALLET_DATA_FILE}: {exc}"
            ) from exc

    # -- balance -------------------------------------------------------------
    def get_balance_wei(self) -> Decimal:
        """Native balance in wei. Raises BalanceUnavailable on read failure."""
        try:
            return self._read_balance_wei()
        except _TRANSIENT_ERRORS as exc:
            raise BalanceUnavailable(f"RPC unavailable while reading balance: {exc}") from exc
        except Exception as exc:
            raise BalanceUnavailable(f"Unexpected error reading balance: {exc}") from exc

    @_network_retry
    def _read_balance_wei(self) -> Decimal:
        return Decimal(self._provider.get_balance())

    def get_eth_balance(self) -> Decimal:
        """Native balance in whole ETH. Raises BalanceUnavailable on failure."""
        return self.get_balance_wei() / WEI_PER_ETH

    def usd_balance(self) -> float:
        """Balance in nominal USD (see ETH_USD_PRICE caveat in module docstring)."""
        return float(self.get_eth_balance() * ETH_USD_PRICE)

    # -- transfer ------------------------------------------------------------
    def transfer(self, to_address: str, eth_amount: Decimal | float | str) -> str:
        """Send native ETH to ``to_address``. Returns the transaction hash.

        Validates the address and amount, refuses to spend past the gas reserve,
        and submits with retry on transient failures. ``native_transfer`` blocks
        until the tx is mined, so a returned hash means the transfer landed.
        Raises TransferError on any validation or on-chain failure.
        """
        if not Web3.is_address(to_address):
            raise TransferError(f"Invalid destination address: {to_address!r}")
        to_checksum = Web3.to_checksum_address(to_address)

        try:
            amount = Decimal(str(eth_amount))
        except (ArithmeticError, ValueError) as exc:
            raise TransferError(f"Invalid amount: {eth_amount!r}") from exc
        if amount <= 0:
            raise TransferError(f"Transfer amount must be positive, got {amount}")

        try:
            spendable = self.get_eth_balance() - GAS_RESERVE_ETH
        except BalanceUnavailable as exc:
            raise TransferError(f"Cannot verify funds before transfer: {exc}") from exc
        if amount > spendable:
            raise TransferError(
                f"Insufficient funds: need {amount} ETH but only {spendable} ETH "
                f"is spendable after the {GAS_RESERVE_ETH} ETH gas reserve."
            )

        try:
            tx_hash = self._submit_transfer(to_checksum, amount)
        except _TRANSIENT_ERRORS as exc:
            raise TransferError(f"Network failure during transfer: {exc}") from exc
        except Exception as exc:
            raise TransferError(f"Transfer rejected: {exc}") from exc

        logger.info("Transferred %s ETH to %s (tx %s)", amount, to_checksum, tx_hash)
        return tx_hash

    @_network_retry
    def _submit_transfer(self, to_checksum: str, amount: Decimal) -> str:
        return self._provider.native_transfer(to=to_checksum, value=amount)

    def usd_to_eth(self, usd_amount: Decimal | float | str) -> Decimal:
        """Convert a USD figure to ETH using the nominal price (testnet caveat)."""
        return Decimal(str(usd_amount)) / ETH_USD_PRICE

    def transfer_usd(self, to_address: str, usd_amount: Decimal | float | str) -> str:
        """Send an amount specified in USD. Returns the transaction hash."""
        return self.transfer(to_address, self.usd_to_eth(usd_amount))

    # -- funding -------------------------------------------------------------
    def request_faucet(self) -> str | None:
        """Best-effort testnet top-up (1 claim / 24h). Returns tx hash or None.

        Never raises: faucet limits/outages are expected and must not crash the
        survival loop.
        """
        wallet = getattr(self._provider, "_wallet", None)
        if wallet is None:
            logger.warning("Faucet unavailable: underlying wallet not accessible")
            return None
        try:
            faucet_tx = wallet.faucet(asset_id="eth")
            faucet_tx.wait()
            tx_hash = faucet_tx.transaction_hash
            logger.info("Faucet funded %s (tx %s)", self.address, tx_hash)
            return tx_hash
        except Exception as exc:  # rate-limited, offline, etc. — all non-fatal
            logger.warning("Faucet request failed (non-fatal): %s", exc)
            return None


if __name__ == "__main__":
    # Smoke test: python wallet.py  (requires CDP_* env vars set)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    w = Wallet()
    print("Address:", w.address)
    try:
        print("Balance:", w.get_eth_balance(), "ETH", f"(~${w.usd_balance():.2f} nominal)")
    except BalanceUnavailable as e:
        print("Balance read failed:", e)
