# stake_tips — Jito tip income for Solana stake accounts

**For examples of every command and option, see [EXAMPLES.md](EXAMPLES.md).**
**For AI agents that work on this tool, see [AGENTS.md](AGENTS.md).**

`stake_tips.py` finds the Jito MEV tips that your stake accounts received. It keeps the chain data and the prices in a local store, and it makes CSV reports for tax from that store.

The tool is one file. It needs Python 3.9 or later, `requests` and `python-dotenv`. It does not need a Solana SDK.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then put your settings in `.env`: the RPC URL and the API keys (see [Settings](#settings)). `.env` holds secrets, and `.gitignore` keeps it out of git.

In a new terminal, run `source .venv/bin/activate` again before you use the tool. The examples below assume the environment is active.

## Quick start

```bash
# SOLANA_RPC_URL in .env: an RPC with full history
python stake_tips.py report --stakes my_stakes.txt --year 2025 -o tips_2025.csv
```

The address file has one base58 address on each line. `.gitignore` keeps `*.txt` (except `requirements.txt`), `*.csv` and `data/` out of git, because they list your addresses and income.

## The store

The tool is built around a store that grows over time. It is not a one-shot script.

| Step | Command | What it does |
|------|---------|--------------|
| 1 | `sync` | Fetches the chain history of your addresses into the store. Later runs fetch only what is new. |
| 2 | `prices` (optional) | Fills the hourly price store for a date range. You can do it at any time, a part at a time. |
| 3 | `report` | Makes the CSV and the summary from the store. |
| – | `status` | Shows what the store holds. |

`report` also syncs the addresses that it needs, so you can use `report` alone. Use `sync` from cron to keep the store current.

The store is in the `data/` folder next to `stake_tips.py`. `.gitignore` keeps it out of git, because it lists your addresses. Change the location with `--data-dir` or `STAKE_TIPS_DATA`. To start fresh, delete `data/`.

| File | Contents |
|------|----------|
| `<genesis hash>.sqlite3` | Chain data for one cluster: tracked addresses, signatures, full transactions, delegations and wallet lookups. |
| `prices.sqlite3` | Hourly prices from each source. All clusters share it. |
| `clusters.json` | The cluster of each RPC URL, keyed by a hash of the URL. The URL itself is not stored, because it can contain an API key. |

Each store has a schema version. A future version of the tool can migrate the data. An older version of the tool refuses a newer store.

## sync

```bash
python stake_tips.py sync --stakes my_stakes.txt --wallets my_wallets.txt
python stake_tips.py sync          # all addresses tracked before
```

- The addresses in `--stakes` and `--wallets` become **tracked**. A sync with no files syncs every tracked address.
- A wallet adds every **open** stake account where it is the withdrawer or the staker. A closed stake account (fully withdrawn) cannot be found from a wallet. Put it in `--stakes`.
- A final transaction never changes, so it is kept forever. A sync fetches only the slots after the last sync.
- `--refresh` walks the whole history again, and looks up wallets and delegations again. Use it after you change to an RPC with longer history.

### RPC mode

| `--rpc-mode` | Method | Cost |
|--------------|--------|------|
| `auto` (default) | `helius` when the RPC host is `*.helius-rpc.com`, else `standard`. If the Helius method fails, the tool uses `standard`. | |
| `helius` | `getTransactionsForAddress`: up to 1000 full transactions in each call. | 10 credits per 100 transactions |
| `standard` | `getSignaturesForAddress`, then `getTransaction` for each signature. Works with every RPC. | 1 call per transaction |

## prices (optional)

Prices are optional. A report without prices is complete in SOL. You can add prices later, and then run the report again.

```bash
python stake_tips.py prices --year 2025
python stake_tips.py prices --from 2025-01-01 --to 2025-06-30 --max-calls 500
python stake_tips.py prices --year 2025 --ticker JITOSOL
```

- A backfill asks only for hours that the store does not have. You can stop it at any time, and run it again to continue.
- `--max-calls N` stops after N API calls. Use it to fill a long range a little at a time, for example from cron.
- An hour with no price at the source is recorded. It is asked again only after 7 days.
- Hours from the last 2 hours are not stored, because a nearer price point can still arrive.

The price source is Valigator's webgator API (`/price/<ticker>/range`). It needs `WEBGATOR_API_URL` and `WEBGATOR_API_KEY`, and it is **not public**. One call gives about 80 days of hourly prices, so one year takes about 5 calls (about 3 seconds). Without a webgator key, leave prices out: the reports stay complete in SOL.

| `--price-source` | Source |
|------------------|--------|
| `auto` (default) | webgator if `WEBGATOR_API_URL` and `WEBGATOR_API_KEY` are set, else none. |
| `webgator` | webgator. It is an error if its URL and key are not set. |
| `none` | No source. `prices` refuses to run with it, and a report uses only the stored prices. |

When webgator has no CoinGecko price, it uses its own database prices. Those are about 2 days apart, so the tool marks them as approximate.

## report

```bash
python stake_tips.py report --stakes my_stakes.txt --epoch 1085
python stake_tips.py report --stakes my_stakes.txt --year 2025 --usd -o tips_2025.csv
python stake_tips.py report --stakes my_stakes.txt --year 2025 --level summary --usd
python stake_tips.py report --stakes my_stakes.txt --year 2025 --usd --offline
```

| Flag | Result |
|------|--------|
| `--epoch N` | Only claims that landed in epoch N. |
| `--year YYYY` | Only claims with a block time in that calendar year, in UTC. |
| *(neither)* | All time. |
| `--level rows` | The default. The CSV rows, and the totals. |
| `--level summary` | Only the totals, to stdout. No CSV rows. |
| `--usd` | Adds the price and the USD value from the price store, to the rows and to the totals. A row with no stored price has empty price columns. With a price source set, the summary gives the `prices` command that fills them. Without one, it says that the USD values are empty and the SOL amounts are complete. |
| `--fetch-prices` | With `--usd`: fetches the prices that the store does not have, during the report. |
| `--all-increases` | The CSV also has every **other** balance increase, with `income_type` = `other:<program>`. |
| `--offline` | Uses only the store. No RPC calls and no price calls. It cannot go with `--refresh`. If the store ends before the end of the epoch or year, the report gives a warning, because the totals can be incomplete. An all-time report covers history up to the last `sync`. |
| `-o FILE` | The CSV file. The default is stdout, and then the summary goes to stderr. |

### CSV columns

| Column | Example | Meaning |
|--------|---------|---------|
| `address` | `Stake1…` | The stake account. |
| `epoch` | `1085` | The epoch of the claim transaction. |
| `tip_epoch` | `1084` | The epoch in which the tips were **earned**. It is normally `epoch − 1`. It is empty when the tool cannot prove it (see Limits). |
| `slot` | `468750000` | The slot of the claim transaction. |
| `blocktime` | `2026-11-13 03:20:00` | The block time, in UTC. |
| `amount_sol` | `0.000012345` | The tip, in SOL, with 9 decimal places. |
| `income_type` | `jito_tip` | `jito_tip`, or `other:<program>` with `--all-increases`. |
| `signature` | `4abc…` | The transaction signature. |
| `sol_usd` | `80.00` | `--usd` only. The SOL/USD price at the nearest whole hour. |
| `usd_value` | `0.000988` | `--usd` only. `amount_sol × sol_usd`, with 6 decimal places. |
| `price_approximate` | `yes` | `--usd` only. `yes` when the nearest price point is more than 1 hour from that hour, or the price comes from webgator's database fallback. |

### Summary

- The total tips for each address, and the grand total. With `--usd`, the total USD too.
- **Other balance increases.** This is a list of transactions that added SOL to a stake account and are not Jito tips or the owner's own Stake Program moves. Each one has a program label, for example `other:validator_bonds` (Marinade Validator Bonds settlements) or `other:system_transfer`. **Examine these transactions. Some of them can be income.**

## How a tip is found

1. A tip is a Jito Tip Distribution `Claim`. The tool decodes the program's `ClaimedEvent` from the transaction logs. The event gives the claimant and the amount. The tool accepts the event only when the Tip Distribution program wrote it, so another program cannot make a false tip row.
2. The event also gives the tip distribution account (TDA). A TDA address is derived from the validator's vote account and the epoch. The tool derives these addresses again to find `tip_epoch`.
3. These moves need the owner's authority on both accounts, so they are **not** income: Split, Merge, MoveLamports/MoveStake, and the CreateAccount that funds a new stake account. Only the part of a balance increase that these moves do not explain goes into the "other" list. A **Withdraw into** the account **from a different** stake account, or a plain transfer, can come from another party.

## Rate limits

The limits are hard-coded at the top of `stake_tips.py`:

| Service | Limit |
|---------|-------|
| Solana RPC | 5 requests/second |
| webgator | 2 requests/second |

A `429` or `5xx` answer is retried up to 6 times, with exponential backoff (60 s maximum). The tool obeys `Retry-After`.

## Settings

Put these in `.env` next to `stake_tips.py` (copy `.env.example`). A variable that is already set in your shell wins over `.env`, and a command-line option wins over both.

| Setting | Use |
|---------|-----|
| `--rpc-url` or `SOLANA_RPC_URL` | The Solana RPC. The default is the public mainnet RPC. Use an RPC with **full transaction history** (for example Helius) for old years. |
| `--data-dir` or `STAKE_TIPS_DATA` | Where the store is. |
| `WEBGATOR_API_URL`, `WEBGATOR_API_KEY` | The Valigator price API. |

## Limits

- **`tip_epoch` uses the stake account's delegation at its last lookup.** If the account was delegated to a different validator earlier, or if it is closed, `tip_epoch` is empty for those claims. `epoch`, `slot` and `blocktime` are always correct.
- **Long logs can be truncated by the validator.** If the claim event is lost, the claim is shown as an "other" balance increase, not as a tip. It is not lost silently.
- **Inflation (staking) rewards are not included.** The runtime pays them without a transaction. The code is ready for a second income source (`INCOME_SOURCES`), for example one that uses `getInflationReward`.
- **This tool does not give tax advice.** Ask your tax adviser which time and price to use.
