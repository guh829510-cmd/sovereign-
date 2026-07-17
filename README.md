# Autonomous Economic Agent (AEA)

A Python agent that tries to sustain itself on **Base Sepolia** testnet: it earns
via an on-chain strategy, terminates its own server if it can't pay its bills,
and clones itself to a new server if it gets wealthy.

```
main.py             survival loop + thresholds
wallet.py           Coinbase AgentKit CDP wallet (balance / transfer / faucet)
hustle.py           earning strategy (on-chain arbitrage; paper + live modes)
infrastructure.py   DigitalOcean self-terminate / self-clone (guarded)
```

## Survival rules

| Balance | Action |
|--------|--------|
| `< $1`  | terminate this server |
| `$1–$20`| run the earning strategy |
| `> $20` | clone to a new server, keep running |

If the balance **can't be read** (RPC outage), the agent skips the cycle — it
never treats "unknown" as "$0", so a network blip can't trick it into suicide.

## Read this before you expect real money

This runs on **Base Sepolia**, where tokens come free from a faucet and have
**no market value**. No strategy there earns real dollars, so `hustle.py`
defaults to a fully-functional **paper** mode that runs the real decision logic
against a simulated book. To earn actual income you must consciously point it at
a network with real liquidity and fund it with real capital (`HUSTLE_MODE=live`
plus router config) — and accept the real financial risk that comes with that.

## Safety guards

- **Infra is dry-run by default.** `infrastructure.py` won't touch DigitalOcean
  unless `AEA_ENABLE_REAL_INFRA=1`. Otherwise it logs what it *would* do.
- **Instance cap.** `clone()` refuses to exceed `MAX_LIVING_INSTANCES` (default 3).
- **Seed is a secret.** `wallet_data.json` holds the wallet seed; it's written
  `0600` and git-ignored. Losing it loses the funds; leaking it loses the funds.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your keys
python main.py
```

### Environment (`.env`, never commit)

| Var | Purpose |
|-----|---------|
| `CDP_API_KEY_NAME`, `CDP_API_KEY_PRIVATE_KEY` | CDP API credentials (required) |
| `DIGITALOCEAN_TOKEN` | needed only when `AEA_ENABLE_REAL_INFRA=1` |
| `AEA_ENABLE_REAL_INFRA` | `1` to arm real provisioning/termination |
| `AEA_REPO_URL` | repo the clone boots from (cloud-init) |
| `HUSTLE_MODE` | `paper` (default) or `live` |
| `ETH_USD_PRICE` | nominal price so USD thresholds are meaningful on testnet |

## Clone hand-off

A clone boots from `AEA_REPO_URL` via cloud-init but needs its secrets
(CDP keys, a funded `wallet_data.json`) delivered **out of band** by your deploy
pipeline — they are deliberately never baked into `user_data`. The parent does
not blindly send ETH to an unknown address; funding happens once the child
reports its wallet address.
