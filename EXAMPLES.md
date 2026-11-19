# stake_tips — command examples

This file shows every form of every command. For what the tool does and how it works, read [README.md](README.md).

The examples run from the project folder, with the virtual environment active (see [Setup](#setup)).

The address files have one base58 address on each line. A `#` starts a comment:

```text
# my_stakes.txt
<stake account address>   # main stake account
<stake account address>
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then put your RPC URL and API keys in .env
```

In a new terminal, run `source .venv/bin/activate` again first.

`.env` holds secrets. Never commit or share it. A variable that is already set in your shell wins over the value in `.env`, and `--rpc-url` wins over both.

## Options for all commands

| Option | Example |
|--------|---------|
| `--data-dir DIR` | `python stake_tips.py status --data-dir ~/tax/store` |
| `-v`, `--verbose` | `python stake_tips.py sync -v` (shows progress and the RPC call count) |
| `-h`, `--help` | `python stake_tips.py report --help` |

## sync — fetch chain history into the store

```bash
# Stake accounts from a file (they become tracked)
python stake_tips.py sync --stakes my_stakes.txt

# Wallets: adds every open stake account where the wallet is the withdrawer or the staker
python stake_tips.py sync --wallets my_wallets.txt

# Both
python stake_tips.py sync --stakes my_stakes.txt --wallets my_wallets.txt

# No files: sync every address that is tracked already (good for cron)
python stake_tips.py sync

# Walk the whole history again, and look up wallets and delegations again
# (for example, after you change to an RPC with longer history)
python stake_tips.py sync --refresh

# A different RPC for this run only
python stake_tips.py sync --rpc-url https://mainnet.helius-rpc.com/?api-key=YOUR_KEY

# Choose the RPC method
python stake_tips.py sync --rpc-mode auto       # default: Helius method on a Helius URL
python stake_tips.py sync --rpc-mode helius     # getTransactionsForAddress (Helius only)
python stake_tips.py sync --rpc-mode standard   # works with every RPC
```

### Cron

```cron
# Every day at 03:00 UTC: new chain data, then a small part of the price backfill.
# cron does not activate the virtual environment, so call its Python directly.
0 3 * * * cd /path/to/stake-collector && .venv/bin/python stake_tips.py sync
30 3 * * * cd /path/to/stake-collector && .venv/bin/python stake_tips.py prices --year 2025 --max-calls 200
```

## prices — fill the price store (optional)

Give `--year`, or `--from`. You cannot give both.

```bash
# A calendar year (UTC)
python stake_tips.py prices --year 2025

# A date range: --to is a whole day, and the default is now
python stake_tips.py prices --from 2025-01-01 --to 2025-06-30
python stake_tips.py prices --from 2026-01-01

# A budget: stop after 200 API calls, then run the same command again to continue
python stake_tips.py prices --year 2025 --max-calls 200

# Choose the source
python stake_tips.py prices --year 2025 --price-source auto        # default: webgator if its URL and key are set
python stake_tips.py prices --year 2025 --price-source webgator    # an error if the URL or key is missing

# Another token: SOL (default), JITOSOL or 2Z
python stake_tips.py prices --year 2025 --ticker JITOSOL
```

## report — the CSV and the totals

`report` needs `--stakes`, `--wallets` or both.

### Period

```bash
python stake_tips.py report --stakes my_stakes.txt                 # all time
python stake_tips.py report --stakes my_stakes.txt --epoch 1085    # claims that landed in epoch 1085
python stake_tips.py report --stakes my_stakes.txt --year 2025     # claims in 2025 (UTC)
```

### Level and USD

| Command | CSV rows | Totals | USD |
|---------|----------|--------|-----|
| `report --stakes my_stakes.txt --year 2025` | Yes | Yes | No |
| `report --stakes my_stakes.txt --year 2025 --usd` | Yes | Yes | Yes |
| `report --stakes my_stakes.txt --year 2025 --level summary` | No | Yes | No |
| `report --stakes my_stakes.txt --year 2025 --level summary --usd` | No | Yes | Yes |

`--level rows` is the default, so `--level rows --usd` is the same as `--usd`.

### Output

```bash
# CSV to a file; the totals go to the terminal
python stake_tips.py report --stakes my_stakes.txt --year 2025 -o tips_2025.csv

# CSV to stdout (the totals go to stderr, so a pipe gets only the CSV)
python stake_tips.py report --stakes my_stakes.txt --year 2025 > tips_2025.csv

# Totals only: they go to stdout
python stake_tips.py report --stakes my_stakes.txt --year 2025 --level summary > totals_2025.txt
```

### Prices in a report

```bash
# Use only the prices in the store (default). Rows without a stored price have empty
# price columns. With a price source set, the summary gives the `prices` command
# that fills them.
python stake_tips.py report --stakes my_stakes.txt --year 2025 --usd

# Fetch the prices the store does not have, during the report
python stake_tips.py report --stakes my_stakes.txt --year 2025 --usd --fetch-prices

# The same, with a chosen source
python stake_tips.py report --stakes my_stakes.txt --year 2025 --usd --fetch-prices --price-source webgator
```

### Other balance increases

```bash
# Also put the "other" balance increases in the CSV (income_type other:<program>)
python stake_tips.py report --stakes my_stakes.txt --year 2025 --all-increases -o all_2025.csv
python stake_tips.py report --stakes my_stakes.txt --year 2025 --all-increases --usd -o all_2025.csv
```

### Offline and refresh

```bash
# Only the store: no RPC calls and no price calls
python stake_tips.py report --stakes my_stakes.txt --year 2025 --usd --offline

# Offline with a store from a named RPC (needed when the store has more than one cluster)
python stake_tips.py report --stakes my_stakes.txt --year 2025 --offline --rpc-url https://api.mainnet-beta.solana.com

# Sync the whole history again before the report
python stake_tips.py report --stakes my_stakes.txt --year 2025 --refresh
```

`--offline` and `--refresh` cannot go together.

### A full example

```bash
python stake_tips.py report \
  --wallets my_wallets.txt --stakes closed_stakes.txt \
  --year 2025 --usd --fetch-prices \
  --all-increases -o tips_2025.csv -v
```

## status — what the store holds

```bash
python stake_tips.py status
python stake_tips.py status --data-dir ~/tax/store
python stake_tips.py status --rpc-url https://api.testnet.solana.com   # the store of another cluster
```

## Common workflows

**One year for tax, with USD:**
```bash
python stake_tips.py sync --wallets my_wallets.txt
python stake_tips.py prices --year 2025
python stake_tips.py report --wallets my_wallets.txt --year 2025 --usd -o tips_2025.csv
```

**Prices later:**
```bash
python stake_tips.py report --stakes my_stakes.txt --year 2025 -o tips_2025.csv        # SOL only, now
python stake_tips.py prices --year 2025                                                # any time later
python stake_tips.py report --stakes my_stakes.txt --year 2025 --usd --offline -o tips_2025.csv
```

**Check one epoch:**
```bash
python stake_tips.py report --stakes my_stakes.txt --epoch 1085 --level summary
```
