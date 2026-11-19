# AGENTS.md — stake_tips for agents

This file describes all of `stake_tips.py` for an AI agent that must read, run, change or debug it. Humans: start with [README.md](README.md) (concepts) and [EXAMPLES.md](EXAMPLES.md) (every command form).

All names below are exact identifiers in `stake_tips.py`. Line numbers change, so search for the name.

---

## 1. What the tool does

`stake_tips.py` finds the **Jito MEV tips** that Solana **stake accounts** received, and reports them as CSV and totals for tax. It keeps everything in a **local persistent store** (SQLite), so it grows over time and a repeat run makes no network calls.

- **Input:** stake account addresses (`--stakes FILE`), wallets (`--wallets FILE`, expanded to their stake accounts), and a period (`--epoch N`, `--year YYYY`, or all time).
- **Output:** one CSV row per tip claim (and per earned epoch), totals per address, an optional USD value, and a review list of other balance increases.
- **Not included:** inflation (staking) rewards. They have no transaction. See §11 to add them.

## 2. Hard rules (do not break these)

1. **Standalone.** The tool is one file. Its only dependencies are `requests` and `python-dotenv` (`requirements.txt`). Do not import a Solana SDK or anything else. The PDA and base58 code is pure Python on purpose.
2. **Never print, log or store an RPC URL.** A URL can contain an API key (for example Helius `?api-key=`). `clusters.json` keys on `sha256(url)[:16]`. Error messages name the RPC method, never the URL.
3. **Never commit or share `.env` or `data/`.** `.env` holds keys; `data/` (the store) lists the tracked addresses. `.gitignore` covers `.env`, `data/`, `*.sqlite3`, `*.csv` (reports hold addresses and income), `*.txt` except `requirements.txt` (address files) and `.venv/`. Never put a real address, signature or claim from your own accounts in the code, tests or docs: use made-up values.
4. **A tip is only an event that the Tip Distribution program logged** (§6.1). Never trust a `Program data:` line from another program.
5. **Never drop income silently.** Anything that raised a balance and is not proven to be the owner's own money goes to the "other" list (§6.3). Anything that cannot be placed in a year is listed in the summary (`unknown_time`).
6. **Never mark data as complete unless it is.** A sync position must not move past data that was not stored (`_walk_helius` raises on a bad answer). "No data" for a price is stored only after a valid answer (§7).
7. **Prices are optional.** A price failure during a report is a warning, never a crash (`add_prices`). Reports are always complete in SOL.
8. **Hard-coded rate limits** (§9) stay. Each HTTP request goes through a `RateLimiter`.
9. **Keep Python 3.9 syntax** in `stake_tips.py` (no `match`, no parenthesized context managers, no `X | Y` types).

## 3. Files

| File | Purpose |
|------|---------|
| `stake_tips.py` | The whole tool. |
| `requirements.txt` | `requests`, `python-dotenv`. |
| `.env.example` | The settings, with no values. Copy to `.env`. |
| `.env` | Local settings and keys. Never commit or share it. |
| `.gitignore` | Keeps secrets (`.env`) and user data (`data/`, `*.sqlite3`, `*.csv`, `*.txt`) out of git. |
| `data/` | The store (§8). Made on the first run; git-ignored. |
| `ruff.toml` | Lint rules and the Python 3.9 target. |
| `README.md` | Concepts, flags, limits (for humans). |
| `EXAMPLES.md` | Every command form. |
| `AGENTS.md` | This file. |
| `tests/test_stake_tips.py` | All tests. |

## 4. Settings

`load_env_file()` loads the `.env` next to `stake_tips.py` (`ENV_FILE`) **before** `parse_args()`, because argument defaults read the environment. Precedence: command-line option > variable already in the shell > `.env`.

| Variable | Used by | Default |
|----------|---------|---------|
| `SOLANA_RPC_URL` | `open_chain` (`--rpc-url` wins) | `DEFAULT_RPC_URL` (public mainnet) |
| `WEBGATOR_API_URL`, `WEBGATOR_API_KEY` | `make_price_source` | none: no prices |
| `STAKE_TIPS_DATA` | `--data-dir` default | `DEFAULT_DATA_DIR` = `data/` next to the script |

## 5. Architecture

```
main()
 ├─ load_env_file()             .env → os.environ (no override)
 ├─ parse_args()                subcommand + options
 └─ COMMANDS[cmd](args, now)
     ├─ cmd_sync    open_chain → resolve_addresses → load_epoch_schedule → sync_all
     ├─ cmd_prices  open_prices → make_price_source → PriceBook.backfill
     ├─ cmd_report  open_chain → resolve_addresses → ensure_synced (each address)
     │              → INCOME_SOURCES (collect_jito_tips) → add_prices (if --usd)
     │              → write_csv / write_summary
     └─ cmd_status  ChainStore.counts + PriceStore.coverage
```

Layers, from the bottom up:

| Layer | Names |
|-------|-------|
| Encoding and PDA | `b58encode`, `b58decode`, `is_pubkey`, `is_on_curve`, `find_program_address`, `tip_distribution_account` |
| HTTP | `RateLimiter`, `http_call`, `_retry_after`, `HttpError` |
| RPC | `Rpc`, `OfflineRpc`, `RpcError`, `OfflineError`, `use_helius` |
| Stores | `_Sqlite`, `ChainStore`, `PriceStore`, `StoreError`, `resolve_genesis`, `chain_store_path` |
| Time | `EpochSchedule`, `load_epoch_schedule`, `year_bounds`, `Period` |
| Sync | `_walk_standard`, `_walk_helius`, `sync_address`, `ensure_synced`, `fetch_transaction`, `stake_voter`, `wallet_stake_accounts`, `sync_all` |
| Classification | `Claim`, `decode_claim_events`, `owner_moves_into`, `lamport_sources`, `stake_authorities_set`, `program_label`, `TxResult`, `classify`, `match_tip_epoch` |
| Prices | `hour_of`, `nearest_points`, `NoPrices`, `WebgatorPrices`, `PriceBook`, `BackfillResult`, `make_price_source`, `PriceError` |
| Report | `IncomeRow`, `Report`, `collect_jito_tips`, `INCOME_SOURCES`, `add_prices`, `usd_value`, `format_sol`, `format_time`, `write_csv`, `write_summary` |
| CLI | `read_address_file`, `parse_date`, `parse_args`, `open_chain`, `open_prices`, `resolve_addresses`, `cmd_*`, `COMMANDS`, `load_env_file`, `main` |

## 6. How income is found

### 6.1 Jito tip claims

On chain (checked on mainnet, 2026-11): a claim is a top-level Router `ClaimWithPayer` (`RouterBmuRBkPUbgEDMtdvTZ75GBdSREZR5uGUxxxpb`). It calls the Tip Distribution program (`TIP_DISTRIBUTION_PROGRAM` = `4R3gSG8BpU4t19KYj8CfnbtRpnT8gtk4dvTHxVRwc2r7`) `Claim` as an inner instruction. That program logs an Anchor **ClaimedEvent** in a `Program data: <base64>` line:

| Bytes | Field |
|-------|-------|
| 0–7 | `CLAIMED_EVENT_DISCRIMINATOR` = `sha256("event:ClaimedEvent")[:8]` |
| 8–39 | tip distribution account (TDA) |
| 40–71 | payer |
| 72–103 | claimant (the stake account) |
| 104–111 | amount, u64 little-endian, lamports |

`decode_claim_events(log_messages)` follows the invoke stack (`Program X invoke [n]` / `Program X success` / `Program X failed:`) and accepts a `Program data:` line only when the **top of the stack is the Tip Distribution program** and the data is exactly `CLAIMED_EVENT_LEN` (112) bytes with the right discriminator. If a validator truncates the logs, the event is lost; the claim then shows as an "other" increase (it is not lost silently).

### 6.2 tip_epoch

A TDA is a PDA: `find_program_address([TIP_DISTRIBUTION_SEED, vote_account, epoch.to_bytes(8, "little")], TIP_DISTRIBUTION_PROGRAM)`. `match_tip_epoch(tda, voter, claim_epoch)` derives it for `claim_epoch` down to `claim_epoch - TIP_EPOCH_LOOKBACK` and returns the epoch that matches, or `None`. The voter comes from `stake_voter` (the stake account's delegation, stored). Normally `tip_epoch = epoch - 1`. It is `None` if the account was delegated elsewhere earlier, or is closed. `collect_jito_tips` writes **one row per earned epoch** in a transaction (unmatched claims share a row with an empty `tip_epoch`).

`is_on_curve` is the curve25519-dalek decompression check. A test compares `find_program_address` with `solders` when it is installed.

### 6.3 classify(tx, address) → TxResult

`delta` = `postBalances - preBalances` of the address. Failed transactions and transactions without the address give `kind = "none"`.

1. **Claims for this address exist** → `kind = "jito_tip"`, `tip` = sum of the claims. `other` = `delta - tip - owner_moves_into(...)` if positive (SOL that came with the claim), with `label`.
2. **`delta <= 0`** → `kind = "none"`.
3. **`unexplained = delta - owner_moves_into(tx, address)`**. If `<= 0`, then `kind = "stake_program"` (the owner's own SOL). Else `kind = "other"`, `delta = unexplained`, `label = program_label(tx)`.

`owner_moves_into` counts as the owner's own money only:
- Stake `split` into the address (`newSplitAccount`): `lamports`.
- Stake `moveLamports` / `moveStake` into the address: `lamports`.
- Stake `merge` into the address: the source's **pre-transaction** balance, **only if** the source pays out nowhere else in the transaction (`lamport_sources`). Otherwise the merged amount is unknown, so it is not counted.
- System `createAccount` / `createAccountWithSeed` of the address: `lamports`, **only if** the funder is a staker or withdrawer set by an `initialize` / `initializeChecked` of the address in the same transaction (`stake_authorities_set`).

Everything else is "other": for example a Stake `withdraw` from another party's stake account (Marinade Validator Bonds settlements, `vBoNdEvzMrSai7is21XgVYik65mqtaKXuSdMBJ1xkW4`) or a plain System transfer. `program_label` names the top-level programs (`KNOWN_PROGRAMS`, ignoring `PLUMBING_PROGRAMS`), for example `other:validator_bonds`.

Open follow-up: a `createAccount` funded by the **staker** (not the withdrawer) should go to review too.

## 7. Prices

### 7.1 Source

`WebgatorPrices` calls Valigator's **webgator-api** `GET /price/<ticker>/range?from=&to=` (header `X-API-Key`). It is not public. One call returns every point in up to 80 days (hourly CoinGecko points), as `{"data": {"points": [[unix, usd], ...], "source", "approximate", ...}}`. Tickers: `SOL`, `JITOSOL`, `2Z`.

- `span_sec = WEBGATOR_WINDOW_SEC` = 80 days − 2 × `WEBGATOR_MARGIN_SEC` (74 days). `fetch` asks for a margin of `WEBGATOR_MARGIN_SEC` (3 days, = `EPOCH_FALLBACK_MAX_GAP_SEC`) on each side, so an edge hour still gets its nearest point, even a sparse fallback one, and the request is at most `WEBGATOR_RANGE_MAX_SEC` (the API's limit). `to` is clamped to now, and `from` stays below `to`. A year is still 5 calls.
- **404** → `{}` (no data). **Another non-200**, a missing `points` list, or bad numbers → `PriceError`.
- `approximate: true` in the answer means webgator used its `epoch_detail` database fallback (points ~2 days apart). The tool then uses `EPOCH_FALLBACK_MAX_GAP_SEC` (3 days) for the nearest point and marks every price approximate.
- `nearest_points(points, hours, max_gap)` gives each hour its nearest point within `max_gap` (default `PRICE_MAX_GAP_SEC`, 6 h). It is approximate if the gap is over `PRICE_APPROX_GAP_SEC` (1 h).
- `make_price_source("auto")` → webgator when `WEBGATOR_API_URL` and `WEBGATOR_API_KEY` are set, else `NoPrices`. `cmd_prices` refuses to run with `NoPrices`, because every hour would be stored as "no data". There is **no direct CoinGecko source**; it was removed on purpose (§14).

### 7.2 PriceBook rules

A price is keyed by the hour nearest the timestamp: `hour_of(ts)`.

| Rule | Where |
|------|-------|
| Only **final** hours are stored: `hour + PRICE_FINAL_SEC <= now` (2 h). A newer hour can still get a nearer point. | `_final`, `_record`, `backfill` |
| "No data" is stored as `usd = NULL` with `fetched_at`, and asked again only after `PRICE_RETRY_MISSING_SEC` (7 days). | `missing_hours`, `usd_at` |
| `usd_at` reads the store from **any source** first (`PriceStore.get_any`, preferring the chosen source). It fetches only with `fetch=True` (`report --fetch-prices`), never offline. Unpriced hours are collected in `unpriced_hours` for the summary hint (`unpriced_hint`). The hint gives a `prices` command only when `price_source_configured`, because `prices` refuses to run without a source. | `usd_at` |
| On a fetch, `usd_at` fills the whole window around the hour (`span_sec`), so neighbouring hours are stored too. `_record` writes only hours that have no stored price, so a sparse or empty answer never replaces a good price. | `usd_at`, `_record` |
| `backfill` plans windows over the missing hours (`span_sec`), stops at `max_calls`, and commits after each call, so it resumes. It stores only the hours it asked for. | `backfill` |

`add_prices` turns a `PriceError` or `HttpError` into a warning, sets `book.fetch = False`, and goes on with stored prices.

## 8. Stores

### 8.1 Files in `--data-dir`

| File | Contents |
|------|----------|
| `<genesis hash>.sqlite3` | `ChainStore`: one per cluster. |
| `prices.sqlite3` | `PriceStore`: shared by all clusters. |
| `clusters.json` | `{sha256(rpc_url)[:16]: genesis_hash}`. |

`resolve_genesis` validates that a genesis hash is 32 base58 bytes **before** it names a file (path-traversal guard), both from the RPC and from `clusters.json`. Offline: with no URL, the only known cluster is used; with an unknown URL, it is an `OfflineError` (never the wrong cluster).

### 8.2 Schemas (`CHAIN_SCHEMA`, `PRICE_SCHEMA`)

| Table | Columns | Notes |
|-------|---------|-------|
| `meta` | key, value (JSON) | `schema_version`, `epoch_schedule` |
| `tracked` | address, kind (`stake`/`wallet`), added_at | `sync` with no files uses these |
| `signatures` | address, signature, slot, block_time, failed | `INSERT OR IGNORE` |
| `sync_state` | address, newest_signature, synced_slot, synced_at, chain_time | `chain_time` = block time of `synced_slot` |
| `transactions` | signature, body (jsonParsed JSON) | kept forever (final transactions never change) |
| `stake_voter` | address, voter, fetched_at | kept until `--refresh`; "not delegated" rechecked after `WALLET_LOOKUP_TTL_SEC` |
| `wallet_stakes` | wallet, stakes (JSON list), fetched_at | a new lookup **adds** accounts; closed ones are kept |
| `prices` | source, ticker, hour, usd (NULL = no data), approximate, fetched_at | |

`_Sqlite` writes `schema_version` = `SCHEMA_VERSION` on a new store, and refuses a store with a newer version (`StoreError`). `ChainStore.__init__` adds the `chain_time` column to older stores. **To change a schema:** add the migration in the store's `__init__`, raise `SCHEMA_VERSION` if old code must refuse the new layout, and add a test with an old-layout store.

## 9. Network behaviour

| Constant | Value | Meaning |
|----------|-------|---------|
| `RPC_REQUESTS_PER_SEC` | 5 | Solana RPC limit |
| `WEBGATOR_REQUESTS_PER_SEC` | 2 | webgator limit (calls are latency-bound anyway, ~0.5–0.9 s) |
| `MAX_ATTEMPTS`, `MAX_BACKOFF_SEC` | 6, 60 | retry on 429, 5xx and network errors |
| `HTTP_TIMEOUT_SEC` | 60 | each request |

`http_call` obeys `Retry-After` as seconds or as an HTTP date (`_retry_after`), else waits `2^attempt` seconds.

**Sync** (`sync_address`): first `getSlot` (finalized) and `getBlockTime` of that slot, then:
- **helius** (`_walk_helius`): `getTransactionsForAddress`, `transactionDetails: full`, `jsonParsed`, `maxSupportedTransactionVersion: 1`, `sortOrder: asc`, `filters.status: succeeded`, and `filters.slot.gt` = the last `synced_slot`. Up to 1000 full transactions per call, stored at once. The `paginationToken` is not null on the last page, so a short page also ends the loop.
- **standard** (`_walk_standard`): `getSignaturesForAddress` newest first, `until` = the newest stored signature; then `getTransaction` per signature (`fetch_transaction`, which commits each one).
- `use_helius`: `--rpc-mode auto` uses helius when the host is `helius-rpc.com` or a subdomain of it. If the method fails in auto mode, the tool warns and falls back to standard for the rest of the run.
- `--refresh` walks the whole history again (no `until` / `slot.gt`).

**Coverage** (`Period.covered_by(synced_slot, chain_time, schedule)`): an epoch is covered when its last slot is ≤ `synced_slot`; a year is covered when `chain_time` is past the year's end (the chain's time, not the clock's, because an RPC can lag). `ensure_synced` skips a sync when the period is covered or the last sync is less than `SYNC_FRESH_SEC` old. Offline, it warns when the period is not covered.

**Wallets** (`wallet_stake_accounts`): two `getProgramAccounts` calls on the Stake program, `dataSize` 200, `memcmp` at `WITHDRAWER_OFFSET` (44) and `STAKER_OFFSET` (12), `dataSlice` length 0. Only open accounts can be found.

**Year filter without a time:** if a signature has no `blockTime`, `collect_jito_tips` reads the transaction, then `getBlockTime(slot)` (stored with `set_block_time`), else lists it in `Report.unknown_time`.

## 10. Report output

- `CSV_FIELDS`: address, epoch, tip_epoch, slot, blocktime (`YYYY-MM-DD HH:MM:SS` UTC), amount_sol (9 decimals, `format_sol`), income_type, signature. With `--usd`, `CSV_USD_FIELDS`: sol_usd, usd_value (6 decimals), price_approximate.
- Rows are sorted by address, then slot. `--all-increases` adds the "other" rows (`income_type` = `other:<label>`).
- `write_summary`: totals per address and in total (tips only), USD totals from **unrounded** values (`usd_value(row, exact=True)`), the "other" list, `tip_mismatches`, `unknown_time`, and the hint for unpriced hours (`unpriced_hint`, printed by `cmd_report` after the summary).
- `--level summary`: totals only, to stdout. Else the CSV goes to `-o` or stdout, and the summary goes to stderr.
- `--offline` with `--refresh` is refused.

## 11. How to extend

**A new income type** (for example inflation rewards):
1. Write `collect_<name>(rpc, store, addresses, period, schedule, refresh=False) -> Report` (the same signature as `collect_jito_tips`).
2. Add it to `INCOME_SOURCES`.
3. Give its rows their own `income_type`. `write_summary` totals only `INCOME_JITO_TIP` today, so add totals for the new type there.
4. Store anything fetched (a new table, plus a migration, §8.2), so a rerun needs no RPC. Inflation rewards: `getInflationReward([addresses], {"epoch": N})`, one call per epoch for up to many addresses.

**A new price source:** a class with `name`, `span_sec` (the seconds one call covers; 0 = one hour per call), a `calls` counter, and `fetch(ticker, start_hour, end_hour) -> {hour: (Decimal usd, bool approximate)}`. It must return `{}` for "no data" and raise `PriceError` for a bad answer (never treat a bad answer as no data). Wire it in `make_price_source` and in the `--price-source` choices.

**A new program label:** add it to `KNOWN_PROGRAMS`.

## 12. Tests

- File: `tests/test_stake_tips.py`. It loads `stake_tips.py` with `importlib`.
- No test reaches the network: `FakeRpc` answers RPC methods (it also fakes `getTransactionsForAddress`, including the non-null last-page token), `FakeSession` and `FakeResponse` fake HTTP, `FakeSource` fakes a price source, and `make_tx` / `tip_tx` / `claim_event` / `stake_ix` build jsonParsed transactions.
- `TestCli.setUp` clears the stake_tips variables from the environment and points `ENV_FILE` at a missing file, so a developer's real `.env` cannot change the tests.
- Price tests set `WEBGATOR_REQUESTS_PER_SEC` high, so they do not wait.
- Run, with the virtual environment active (`source .venv/bin/activate`):
  ```bash
  python -m unittest discover -s tests
  ruff check . && ruff format --check .
  ```

## 13. Change checklist

1. Read §2. Keep the tool standalone and Python 3.9 compatible.
2. Add or change tests in `tests/test_stake_tips.py`, with no network and no real user data.
3. Run the tests, and gate the commit on the `OK` line.
4. Run ruff (check and format).
5. Update `README.md`, `EXAMPLES.md` and this file when behaviour or flags change.
6. For a change to stored data: add a migration (§8.2) and test it on an old-layout store.
7. Check live with a scratch `--data-dir`, and never print the RPC URL.

## 14. Decisions (do not undo without a reason)

| Decision | Why |
|----------|-----|
| Helius `getTransfersByAddress` is **not** used | Its docs say it does not report lamport changes from program-owned accounts (Jito claims) or Stake withdraws, so it would miss the income. |
| Helius `getTransactionsForAddress` is used when available | 1000 full transactions per call, about a tenth of the credits of `getTransaction`. |
| Direct CoinGecko was removed | One price path. webgator `/range` is as fast. Users without a webgator key get SOL only. |
| webgator `/range` (not `/at`) | `/at` is one upstream call per hour: about 8,760 calls (~77 min) for a year. `/range` is about 5 calls (~3 s). |
| The prices are optional | Reports stay complete in SOL; prices can be added later with `prices` (a backfill that resumes). |
| `tip_epoch` comes from the PDA, not from `epoch - 1` | Proved, not guessed. Empty when it cannot be proved. |
| "Other" means unexplained, not "not Stake Program" | Validator Bonds pays settlements with a Stake `withdraw`; a simple program rule would hide that income. |
