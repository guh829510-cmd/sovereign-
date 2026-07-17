"""
wallet.py — the agent's money.

Thin, testable wrapper around the Coinbase AgentKit wallet on Base Sepolia.
Everything that touches funds lives here so the rest of the codebase can
reason about "how much do I have?" and "pay this bill" without knowing any
on-chain details.

Responsibilities:
    * create / load a persistent wallet (seed must be persisted across clones)
    * report balance in USD terms (so survival thresholds are meaningful)
    * top up from a testnet faucet when possible
    * send funds (e.g. to fund a freshly cloned agent)
"""

from __future__ import annotations


class Wallet:
    """Wraps an AgentKit-managed wallet. Implemented in Step 3."""

    def usd_balance(self) -> float:
        """Return the current spendable balance expressed in USD."""
        raise NotImplementedError

    def request_faucet(self) -> None:
        """Request testnet funds from a faucet (rate-limited upstream)."""
        raise NotImplementedError

    def send(self, to_address: str, usd_amount: float) -> str:
        """Send funds to another address; return the tx hash."""
        raise NotImplementedError
