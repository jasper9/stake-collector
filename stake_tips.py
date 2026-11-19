#!/usr/bin/env python3
"""Jito tip income for Solana stake accounts, kept in a local store.

Standalone: needs only Python 3.9+, `requests` and `python-dotenv`. See README.md.

Commands:
  sync    fetch chain data for tracked addresses into the store
  prices  backfill the hourly price store, a budget of calls at a time
  report  CSV + summary from the store (syncs and prices what is missing,
          unless --offline)
  status  what the store holds

A tip reaches a stake account as a Jito Tip Distribution `Claim` (usually
inside a Router `ClaimWithPayer`). The program emits an Anchor ClaimedEvent
with the claimant and the amount, which this tool decodes from the logs.
"""

import argparse
import base64
import bisect
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Hard-coded limits. Every request goes through a RateLimiter, and a 429 or
# 5xx answer is retried with backoff (Retry-After is obeyed).
# ---------------------------------------------------------------------------
RPC_REQUESTS_PER_SEC = 5.0  # public mainnet allows ~10/s per IP; Helius free 10/s
WEBGATOR_REQUESTS_PER_SEC = 2.0
MAX_ATTEMPTS = 6
MAX_BACKOFF_SEC = 60
HTTP_TIMEOUT_SEC = 60

SIGNATURE_PAGE = 1000  # getSignaturesForAddress / getTransactionsForAddress maximum
SYNC_FRESH_SEC = 600  # report: a signature list synced this recently is reused
WALLET_LOOKUP_TTL_SEC = 24 * 3600
TIP_EPOCH_LOOKBACK = 10  # claims are matched to tips earned up to 10 epochs before

PRICE_FINAL_SEC = 2 * 3600  # a more recent hour can still get a nearer point
PRICE_MAX_GAP_SEC = 6 * 3600  # no point this near: the hour has no price
PRICE_APPROX_GAP_SEC = 3600  # nearest point further than this: approximate
PRICE_RETRY_MISSING_SEC = 7 * 86400  # an hour with no data is asked again after this
# webgator's epoch_detail fallback has about one point per 2 days: an hour may
# use one up to this far away (as webgator's own /at does), marked approximate.
EPOCH_FALLBACK_MAX_GAP_SEC = 3 * 86400
# Asked for on each side of a window, so an edge hour still gets its nearest
# point, even a sparse fallback one.
WEBGATOR_MARGIN_SEC = EPOCH_FALLBACK_MAX_GAP_SEC
# webgator /price/<ticker>/range answers at most 80 days per call; a window
# plus both margins stays inside that (74 days: a year is still 5 calls).
WEBGATOR_RANGE_MAX_SEC = 80 * 86400
WEBGATOR_WINDOW_SEC = WEBGATOR_RANGE_MAX_SEC - 2 * WEBGATOR_MARGIN_SEC

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
# The store lives next to this script (git-ignored), with the code and .env.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
# Settings and keys (SOLANA_RPC_URL, WEBGATOR_API_KEY, ...) can live here;
# see .env.example. Real environment variables win over the file.
ENV_FILE = Path(__file__).resolve().parent / ".env"
SCHEMA_VERSION = 1

LAMPORTS_PER_SOL = 1_000_000_000

STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TIP_DISTRIBUTION_PROGRAM = "4R3gSG8BpU4t19KYj8CfnbtRpnT8gtk4dvTHxVRwc2r7"
TIP_DISTRIBUTION_SEED = b"TIP_DISTRIBUTION_ACCOUNT"
# Anchor event = sha256("event:ClaimedEvent")[:8], then tip_distribution_account,
# payer, claimant (32 bytes each) and amount (u64 LE): 112 bytes.
CLAIMED_EVENT_DISCRIMINATOR = hashlib.sha256(b"event:ClaimedEvent").digest()[:8]
CLAIMED_EVENT_LEN = 112

# Stake account layout: authorized staker at 12, withdrawer at 44, 200 bytes.
STAKE_ACCOUNT_SIZE = 200
STAKER_OFFSET = 12
WITHDRAWER_OFFSET = 44

INCOME_JITO_TIP = "jito_tip"

# Names for the programs behind an "other" balance increase. Unknown programs
# are shown by their address.
KNOWN_PROGRAMS = {
    "vBoNdEvzMrSai7is21XgVYik65mqtaKXuSdMBJ1xkW4": "validator_bonds",
    "mnspJQyF1KdDEs5c6YJPocYdY1esBgVQFufM2dY9oDk": "marinade_native",
    "RouterBmuRBkPUbgEDMtdvTZ75GBdSREZR5uGUxxxpb": "jito_router",
    TIP_DISTRIBUTION_PROGRAM: "jito_tip_distribution",
    SYSTEM_PROGRAM: "system_transfer",
}
# Programs that never explain where the lamports came from.
PLUMBING_PROGRAMS = {
    "ComputeBudget111111111111111111111111111111",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    STAKE_PROGRAM,
}

VERBOSE = False


def log(message):
    if VERBOSE:
        print(message, file=sys.stderr)


def warn(message):
    print(f"WARNING: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Base58 and program-derived addresses (no Solana SDK needed)
# ---------------------------------------------------------------------------
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {c: i for i, c in enumerate(B58_ALPHABET)}


def b58encode(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58_ALPHABET[rem] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def b58decode(text):
    n = 0
    for ch in text:
        if ch not in B58_INDEX:
            raise ValueError(f"not base58: {text!r}")
        n = n * 58 + B58_INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(text) - len(text.lstrip("1"))
    return b"\0" * pad + body


def is_pubkey(text):
    try:
        return len(b58decode(text)) == 32
    except ValueError:
        return False


_ED_P = 2**255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P


def is_on_curve(point):
    """True when 32 bytes decompress to an ed25519 point (as curve25519-dalek)."""
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= _ED_P:
        return False
    y2 = y * y % _ED_P
    u = (y2 - 1) % _ED_P
    v = (_ED_D * y2 + 1) % _ED_P
    x2 = u * pow(v, _ED_P - 2, _ED_P) % _ED_P
    return x2 == 0 or pow(x2, (_ED_P - 1) // 2, _ED_P) == 1


def find_program_address(seeds, program_id):
    """Return (address, bump) like Pubkey::find_program_address."""
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress"
        ).digest()
        if not is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("no viable bump seed")


def tip_distribution_account(vote_account, epoch):
    """The Tip Distribution Account that holds a validator's tips for an epoch."""
    seeds = [
        TIP_DISTRIBUTION_SEED,
        b58decode(vote_account),
        epoch.to_bytes(8, "little"),
    ]
    return find_program_address(seeds, TIP_DISTRIBUTION_PROGRAM)[0]


# ---------------------------------------------------------------------------
# HTTP with rate limit and retry
# ---------------------------------------------------------------------------
class RateLimiter:
    """Allow at most `rate` calls per second (no bursts)."""

    def __init__(self, rate, clock=time.monotonic, sleep=time.sleep):
        self.interval = 1.0 / rate
        self.clock = clock
        self.sleep = sleep
        self.next_at = 0.0

    def wait(self):
        now = self.clock()
        if now < self.next_at:
            self.sleep(self.next_at - now)
            now = self.next_at
        self.next_at = now + self.interval


class HttpError(Exception):
    pass


def _retry_after(response, attempt, now=time.time):
    """Seconds to wait: Retry-After as seconds or as an HTTP date, else backoff."""
    header = response.headers.get("Retry-After") if response is not None else None
    header = (header or "").strip()
    if header.isdigit():
        return min(int(header), MAX_BACKOFF_SEC)
    if header:
        try:
            when = parsedate_to_datetime(header).timestamp()
        except (TypeError, ValueError):
            when = None
        if when is not None:
            return min(max(int(when - now()) + 1, 1), MAX_BACKOFF_SEC)
    return min(2**attempt, MAX_BACKOFF_SEC)


def http_call(session, limiter, method, url, sleep=time.sleep, **kwargs):
    """One HTTP request through the limiter. Retries 429, 5xx and network errors."""
    last = None
    for attempt in range(MAX_ATTEMPTS):
        limiter.wait()
        response = None
        try:
            response = session.request(method, url, timeout=HTTP_TIMEOUT_SEC, **kwargs)
        except requests.RequestException as exc:
            last = f"network error: {exc.__class__.__name__}"
        else:
            if response.status_code == 429 or response.status_code >= 500:
                last = f"HTTP {response.status_code}"
            else:
                return response
        if attempt + 1 < MAX_ATTEMPTS:
            delay = _retry_after(response, attempt)
            log(f"  {last}; retry in {delay}s")
            sleep(delay)
    raise HttpError(f"{last} after {MAX_ATTEMPTS} attempts")


class RpcError(Exception):
    pass


class OfflineError(RpcError):
    """--offline and the store does not hold what is needed."""


class Rpc:
    """Minimal Solana JSON-RPC client. The URL is never printed (it can hold a key).

    mode: "standard" (getSignaturesForAddress + getTransaction), "helius"
    (getTransactionsForAddress: up to 1000 full transactions per call), or
    "auto" (helius when the host is Helius, standard if that method fails).
    """

    def __init__(self, url, session=None, limiter=None, sleep=time.sleep, mode="auto"):
        self.url = url
        self.session = session or requests.Session()
        self.limiter = limiter or RateLimiter(RPC_REQUESTS_PER_SEC)
        self.sleep = sleep
        self.mode = mode
        self.calls = 0

    def call(self, method, params):
        self.calls += 1
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = http_call(
            self.session, self.limiter, "POST", self.url, sleep=self.sleep, json=body
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RpcError(f"{method}: HTTP {response.status_code}, not JSON") from exc
        if "error" in payload:
            raise RpcError(
                f"{method}: {payload['error'].get('message', payload['error'])}"
            )
        if "result" not in payload:
            raise RpcError(f"{method}: no result")
        return payload["result"]


class OfflineRpc:
    """Stands in for Rpc with --offline: every call fails, so only the store is read."""

    url = None
    mode = "standard"
    calls = 0

    def call(self, method, params):
        raise OfflineError(f"--offline: {method} is not in the store; run `sync` first")


def use_helius(rpc):
    if rpc.mode == "auto":
        host = urlparse(rpc.url or "").hostname or ""
        return host == "helius-rpc.com" or host.endswith(".helius-rpc.com")
    return rpc.mode == "helius"


# ---------------------------------------------------------------------------
# Persistent stores (SQLite). The chain store is one file per cluster; the
# price store is shared by all clusters. Both carry a schema version.
# ---------------------------------------------------------------------------
CHAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tracked (
    address TEXT PRIMARY KEY, kind TEXT NOT NULL, added_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS signatures (
    address TEXT NOT NULL, signature TEXT NOT NULL, slot INTEGER NOT NULL,
    block_time INTEGER, failed INTEGER NOT NULL,
    PRIMARY KEY (address, signature));
CREATE TABLE IF NOT EXISTS sync_state (
    address TEXT PRIMARY KEY, newest_signature TEXT,
    synced_slot INTEGER NOT NULL, synced_at INTEGER NOT NULL, chain_time INTEGER);
CREATE TABLE IF NOT EXISTS transactions (signature TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stake_voter (
    address TEXT PRIMARY KEY, voter TEXT, fetched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS wallet_stakes (
    wallet TEXT PRIMARY KEY, stakes TEXT NOT NULL, fetched_at INTEGER NOT NULL);
"""

PRICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- usd NULL: the source had no point near that hour when fetched_at was written.
CREATE TABLE IF NOT EXISTS prices (
    source TEXT NOT NULL, ticker TEXT NOT NULL, hour INTEGER NOT NULL,
    usd TEXT, approximate INTEGER NOT NULL, fetched_at INTEGER NOT NULL,
    PRIMARY KEY (source, ticker, hour));
"""


class StoreError(Exception):
    pass


class _Sqlite:
    def __init__(self, path, schema):
        self.path = Path(path)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(schema)
        version = self.get_meta("schema_version")
        if version is None:
            self.set_meta("schema_version", SCHEMA_VERSION)
            self.commit()
        elif version > SCHEMA_VERSION:
            raise StoreError(
                f"{path} has schema {version}; this tool knows {SCHEMA_VERSION}."
                " Use a newer stake_tips.py."
            )

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.close()

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )


class ChainStore(_Sqlite):
    def __init__(self, path):
        super().__init__(path, CHAIN_SCHEMA)
        # chain_time came after the first stores were made: add it to them.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sync_state)")}
        if "chain_time" not in columns:
            self.db.execute("ALTER TABLE sync_state ADD COLUMN chain_time INTEGER")
            self.commit()

    def track(self, addresses, kind, now):
        self.db.executemany(
            "INSERT OR IGNORE INTO tracked (address, kind, added_at) VALUES (?, ?, ?)",
            [(a, kind, now) for a in addresses],
        )

    def tracked(self, kind):
        return [
            row[0]
            for row in self.db.execute(
                "SELECT address FROM tracked WHERE kind = ? ORDER BY added_at, address",
                (kind,),
            )
        ]

    def sync_state(self, address):
        return self.db.execute(
            "SELECT newest_signature, synced_slot, synced_at, chain_time"
            " FROM sync_state"
            " WHERE address = ?",
            (address,),
        ).fetchone()

    def save_signatures(
        self, address, rows, newest, synced_slot, synced_at, chain_time=None
    ):
        self.db.executemany(
            "INSERT OR IGNORE INTO signatures"
            " (address, signature, slot, block_time, failed) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    address,
                    r["signature"],
                    r["slot"],
                    r.get("blockTime"),
                    1 if r.get("err") else 0,
                )
                for r in rows
            ],
        )
        self.db.execute(
            "INSERT OR REPLACE INTO sync_state (address, newest_signature,"
            " synced_slot, synced_at, chain_time) VALUES (?, ?, ?, ?, ?)",
            (address, newest, synced_slot, synced_at, chain_time),
        )

    def set_block_time(self, signature, block_time):
        self.db.execute(
            "UPDATE signatures SET block_time = ? WHERE signature = ?",
            (block_time, signature),
        )

    def signatures(self, address):
        return self.db.execute(
            "SELECT signature, slot, block_time FROM signatures"
            " WHERE address = ? AND failed = 0 ORDER BY slot",
            (address,),
        ).fetchall()

    def get_transaction(self, signature):
        row = self.db.execute(
            "SELECT body FROM transactions WHERE signature = ?", (signature,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def has_transaction(self, signature):
        return (
            self.db.execute(
                "SELECT 1 FROM transactions WHERE signature = ?", (signature,)
            ).fetchone()
            is not None
        )

    def put_transaction(self, signature, tx):
        self.db.execute(
            "INSERT OR REPLACE INTO transactions (signature, body) VALUES (?, ?)",
            (signature, json.dumps(tx, separators=(",", ":"))),
        )

    def get_voter(self, address):
        return self.db.execute(
            "SELECT voter, fetched_at FROM stake_voter WHERE address = ?", (address,)
        ).fetchone()

    def put_voter(self, address, voter, fetched_at):
        self.db.execute(
            "INSERT OR REPLACE INTO stake_voter (address, voter, fetched_at)"
            " VALUES (?, ?, ?)",
            (address, voter, fetched_at),
        )

    def get_wallet(self, wallet):
        return self.db.execute(
            "SELECT stakes, fetched_at FROM wallet_stakes WHERE wallet = ?", (wallet,)
        ).fetchone()

    def put_wallet(self, wallet, stakes, fetched_at):
        self.db.execute(
            "INSERT OR REPLACE INTO wallet_stakes (wallet, stakes, fetched_at)"
            " VALUES (?, ?, ?)",
            (wallet, json.dumps(stakes), fetched_at),
        )

    def counts(self):
        one = self.db.execute
        return {
            "tracked stake accounts": one(
                "SELECT COUNT(*) FROM tracked WHERE kind = 'stake'"
            ).fetchone()[0],
            "tracked wallets": one(
                "SELECT COUNT(*) FROM tracked WHERE kind = 'wallet'"
            ).fetchone()[0],
            "addresses synced": one("SELECT COUNT(*) FROM sync_state").fetchone()[0],
            "signatures": one("SELECT COUNT(*) FROM signatures").fetchone()[0],
            "transactions": one("SELECT COUNT(*) FROM transactions").fetchone()[0],
        }


class PriceStore(_Sqlite):
    def __init__(self, path):
        super().__init__(path, PRICE_SCHEMA)

    def get(self, source, ticker, hour):
        """(usd or None, approximate, fetched_at), or None if never fetched."""
        row = self.db.execute(
            "SELECT usd, approximate, fetched_at FROM prices"
            " WHERE source = ? AND ticker = ? AND hour = ?",
            (source, ticker, hour),
        ).fetchone()
        if row is None:
            return None
        return (Decimal(row[0]) if row[0] is not None else None, bool(row[1]), row[2])

    def put(self, source, ticker, hour, usd, approximate, fetched_at):
        self.db.execute(
            "INSERT OR REPLACE INTO prices"
            " (source, ticker, hour, usd, approximate, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                source,
                ticker,
                hour,
                None if usd is None else str(usd),
                1 if approximate else 0,
                fetched_at,
            ),
        )

    def get_any(self, ticker, hour, preferred=None):
        """(usd, approximate) from any source that has a price for the hour,
        `preferred` first; None if no source has one."""
        row = self.db.execute(
            "SELECT usd, approximate FROM prices WHERE ticker = ? AND hour = ?"
            " AND usd IS NOT NULL ORDER BY source = ? DESC, source LIMIT 1",
            (ticker, hour, preferred),
        ).fetchone()
        return (Decimal(row[0]), bool(row[1])) if row else None

    def known(self, source, ticker, start, end):
        """{hour: (usd or None, fetched_at)} for stored hours in [start, end]."""
        return {
            row[0]: (row[1], row[2])
            for row in self.db.execute(
                "SELECT hour, usd, fetched_at FROM prices WHERE source = ?"
                " AND ticker = ? AND hour BETWEEN ? AND ?",
                (source, ticker, start, end),
            )
        }

    def coverage(self):
        """[(source, ticker, year, hours with a price, hours with no data)]."""
        return self.db.execute(
            "SELECT source, ticker, CAST(strftime('%Y', hour, 'unixepoch') AS INTEGER),"
            " SUM(usd IS NOT NULL), SUM(usd IS NULL) FROM prices"
            " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ).fetchall()


def resolve_genesis(data_dir, rpc):
    """The cluster's genesis hash, which names the chain store. It is saved for
    each RPC URL (under a digest of the URL, never the URL itself, which can
    hold an API key), so a rerun, or --offline, does not ask again."""
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    index_file = directory / "clusters.json"
    try:
        index = json.loads(index_file.read_text())
    except (OSError, ValueError):
        index = {}
    url_key = hashlib.sha256((rpc.url or "").encode()).hexdigest()[:16]
    genesis = index.get(url_key)
    # The hash names a file, so a value that is not 32 base58 bytes (e.g.
    # "../x" from a hostile RPC or an edited index) is never used as one.
    if not (isinstance(genesis, str) and is_pubkey(genesis)):
        if isinstance(rpc, OfflineRpc):
            # Only with no URL at all may the sole known cluster stand in: an
            # unknown URL can be another cluster, whose data this is not.
            if rpc.url:
                raise OfflineError(
                    "--offline: this RPC URL was never synced; give the URL of an"
                    " earlier sync, or none"
                )
            known = [g for g in index.values() if isinstance(g, str) and is_pubkey(g)]
            if len(set(known)) != 1:
                raise OfflineError("--offline: give --rpc-url used by an earlier sync")
            return known[0]
        genesis = rpc.call("getGenesisHash", [])
        if not (isinstance(genesis, str) and is_pubkey(genesis)):
            raise RpcError("getGenesisHash: not a base58 hash")
        index[url_key] = genesis
        index_file.write_text(json.dumps(index, indent=1))
    return genesis


def chain_store_path(data_dir, rpc):
    return Path(data_dir) / f"{resolve_genesis(data_dir, rpc)}.sqlite3"


# ---------------------------------------------------------------------------
# Epochs and time ranges
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EpochSchedule:
    slots_per_epoch: int
    first_normal_epoch: int
    first_normal_slot: int

    def epoch_of(self, slot):
        if slot < self.first_normal_slot:
            # Warm-up epochs (not on mainnet): lengths double from 32 slots.
            epoch, start, length = 0, 0, 32
            while start + length <= slot:
                start += length
                length *= 2
                epoch += 1
            return epoch
        return (
            self.first_normal_epoch
            + (slot - self.first_normal_slot) // self.slots_per_epoch
        )

    def last_slot(self, epoch):
        if epoch < self.first_normal_epoch:
            return 32 * (2 ** (epoch + 1) - 1) - 1
        offset = epoch - self.first_normal_epoch + 1
        return self.first_normal_slot + offset * self.slots_per_epoch - 1


def load_epoch_schedule(rpc, store):
    cached = store.get_meta("epoch_schedule")
    if cached is None:
        result = rpc.call("getEpochSchedule", [])
        cached = {
            "slots_per_epoch": result["slotsPerEpoch"],
            "first_normal_epoch": result["firstNormalEpoch"],
            "first_normal_slot": result["firstNormalSlot"],
        }
        store.set_meta("epoch_schedule", cached)
        store.commit()
    return EpochSchedule(**cached)


def year_bounds(year):
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


@dataclass(frozen=True)
class Period:
    """What to report: one claim epoch, one UTC calendar year, or all time."""

    epoch: int = None
    year: int = None

    def year_bounds(self):
        return year_bounds(self.year)

    def contains(self, slot, block_time, schedule):
        if self.epoch is not None:
            return schedule.epoch_of(slot) == self.epoch
        if self.year is not None:
            start, end = self.year_bounds()
            return block_time is not None and start <= block_time < end
        return True

    def covered_by(self, synced_slot, chain_time, schedule):
        """True when a sync up to finalized `synced_slot`, whose block time is
        `chain_time` (None if unknown), already holds the whole period. The
        chain's time, not the clock's, decides: an RPC can lag behind."""
        if self.epoch is not None:
            return schedule.last_slot(self.epoch) <= synced_slot
        if self.year is not None:
            # A margin for the gap between block time and finalization.
            return chain_time is not None and self.year_bounds()[1] <= chain_time
        return False

    def label(self):
        if self.epoch is not None:
            return f"epoch {self.epoch}"
        if self.year is not None:
            return f"year {self.year} (UTC)"
        return "all time"


# ---------------------------------------------------------------------------
# Sync: chain data into the store
# ---------------------------------------------------------------------------
TX_OPTIONS = {
    "encoding": "jsonParsed",
    "commitment": "finalized",
    "maxSupportedTransactionVersion": 1,
}


def _walk_standard(rpc, address, until):
    """getSignaturesForAddress, newest first, back to `until` (or all history)."""
    rows, before = [], None
    while True:
        options = {"limit": SIGNATURE_PAGE, "commitment": "finalized"}
        if before:
            options["before"] = before
        if until:
            options["until"] = until
        page = rpc.call("getSignaturesForAddress", [address, options])
        rows.extend(page)
        if len(page) < SIGNATURE_PAGE:
            return rows
        before = page[-1]["signature"]


def _walk_helius(rpc, store, address, after_slot):
    """getTransactionsForAddress (Helius), oldest first, after `after_slot`.
    Full transactions come in the same call and go straight into the store."""
    options = dict(
        TX_OPTIONS,
        transactionDetails="full",
        sortOrder="asc",
        limit=SIGNATURE_PAGE,
        filters={"status": "succeeded"},
    )
    if after_slot is not None:
        options["filters"]["slot"] = {"gt": after_slot}
    rows = []
    while True:
        result = rpc.call("getTransactionsForAddress", [address, options])
        # A malformed answer must fail here: taken as "no history", it would
        # move the sync position past transactions never stored.
        page = result.get("data") if isinstance(result, dict) else None
        if not isinstance(page, list):
            raise RpcError("getTransactionsForAddress: answer has no data list")
        for item in page:
            signature = item["transaction"]["signatures"][0]
            store.put_transaction(signature, item)
            rows.append(
                {
                    "signature": signature,
                    "slot": item["slot"],
                    "blockTime": item.get("blockTime"),
                    "err": item["meta"].get("err"),
                }
            )
        # The token is not null on the last page, so a short page also ends it.
        token = result.get("paginationToken")
        if len(page) < SIGNATURE_PAGE or not token:
            return rows
        options["paginationToken"] = token


def sync_address(rpc, store, address, refresh, now):
    """Bring the stored history of `address` up to date. Returns the new rows.

    --refresh walks the whole history again (duplicates are ignored), so a
    store filled from a short-history RPC is completed from an archival one.
    """
    state = store.sync_state(address)
    newest = state[0] if state and not refresh else None
    after_slot = state[1] if state and not refresh else None
    # The finalized slot before the walk: every signature at or below it is final.
    synced_slot = rpc.call("getSlot", [{"commitment": "finalized"}])
    try:
        chain_time = rpc.call("getBlockTime", [synced_slot])
    except RpcError:
        chain_time = None  # e.g. a skipped slot: the year stays "not covered"
    rows = None
    if use_helius(rpc):
        try:
            rows = _walk_helius(rpc, store, address, after_slot)
            newest = rows[-1]["signature"] if rows else newest
        except RpcError as exc:
            if rpc.mode != "auto":
                raise
            warn(f"getTransactionsForAddress failed ({exc}); using standard RPC")
            rpc.mode = "standard"
    if rows is None:
        rows = _walk_standard(rpc, address, newest)
        newest = rows[0]["signature"] if rows else newest
    if newest is None and state:
        newest = state[0]
    log(f"{address}: {len(rows)} new signature(s)")
    store.save_signatures(address, rows, newest, synced_slot, now, chain_time)
    store.commit()
    return rows


def ensure_synced(rpc, store, address, schedule, period, refresh, now):
    """report: sync only when the store may not hold the whole period."""
    state = store.sync_state(address)
    if isinstance(rpc, OfflineRpc):
        if not state:
            raise OfflineError(f"--offline: {address} was never synced")
        # The store may end before the period does: say so, as the totals
        # can then be incomplete.
        _, synced_slot, synced_at, chain_time = state
        if period.epoch is not None or period.year is not None:
            if not period.covered_by(synced_slot, chain_time, schedule):
                warn(
                    f"{address}: the store holds history only up to"
                    f" {format_time(chain_time or synced_at)} UTC"
                    f" (slot {synced_slot}); the"
                    f" {period.label()} report can be incomplete. Run `sync`."
                )
        return
    if state and not refresh:
        _, synced_slot, synced_at, chain_time = state
        if period.covered_by(synced_slot, chain_time, schedule):
            log(f"{address}: store already holds {period.label()}")
            return
        if now - synced_at < SYNC_FRESH_SEC:
            log(f"{address}: synced {now - synced_at}s ago, not re-synced")
            return
    sync_address(rpc, store, address, refresh, now)


def fetch_transaction(rpc, store, signature):
    tx = store.get_transaction(signature)
    if tx is None:
        tx = rpc.call("getTransaction", [signature, TX_OPTIONS])
        if tx is None:
            raise RpcError(
                f"getTransaction: {signature} not found (RPC history too short?)"
            )
        store.put_transaction(signature, tx)
        # Commit each fetch, so an interrupted sync resumes from here.
        store.commit()
    return tx


def stake_voter(rpc, store, address, refresh, now):
    """The vote account the stake account delegated to when last looked up
    (None if not delegated). A voter is kept until --refresh, so a rerun needs
    no RPC; "not delegated" is checked again after WALLET_LOOKUP_TTL_SEC, as
    the account may have been delegated since."""
    row = store.get_voter(address)
    if row and not refresh and (row[0] or now - row[1] < WALLET_LOOKUP_TTL_SEC):
        return row[0]
    info = rpc.call("getAccountInfo", [address, {"encoding": "jsonParsed"}])
    try:
        voter = info["value"]["data"]["parsed"]["info"]["stake"]["delegation"]["voter"]
    except (KeyError, TypeError):
        # Closed, never delegated, or not a stake account: no voter, so
        # tip_epoch stays empty for this address.
        voter = None
    store.put_voter(address, voter, now)
    store.commit()
    return voter


def wallet_stake_accounts(rpc, store, wallet, refresh, now):
    """Stake accounts with `wallet` as the withdrawer or the staker: the open
    ones now, plus every one found by an earlier lookup."""
    row = store.get_wallet(wallet)
    stale = row is None or refresh or now - row[1] >= WALLET_LOOKUP_TTL_SEC
    if row and (not stale or isinstance(rpc, OfflineRpc)):
        return json.loads(row[0])
    found = set()
    for offset in (WITHDRAWER_OFFSET, STAKER_OFFSET):
        result = rpc.call(
            "getProgramAccounts",
            [
                STAKE_PROGRAM,
                {
                    "encoding": "base64",
                    "dataSlice": {"offset": 0, "length": 0},
                    "filters": [
                        {"dataSize": STAKE_ACCOUNT_SIZE},
                        {"memcmp": {"offset": offset, "bytes": wallet}},
                    ],
                },
            ],
        )
        found.update(item["pubkey"] for item in result)
    # Keep every account found before: a closed account is no longer in
    # getProgramAccounts, but its past income still belongs to this wallet.
    if row:
        found.update(json.loads(row[0]))
    stakes = sorted(found)
    store.put_wallet(wallet, stakes, now)
    store.commit()
    return stakes


def sync_all(rpc, store, addresses, refresh, now):
    """sync: whole history and every transaction for each address."""
    new_rows = 0
    for address in addresses:
        rows = sync_address(rpc, store, address, refresh, now)
        new_rows += len(rows)
        for signature, _, _ in store.signatures(address):
            if not store.has_transaction(signature):
                fetch_transaction(rpc, store, signature)
        stake_voter(rpc, store, address, refresh, now)
        store.commit()
    return new_rows


# ---------------------------------------------------------------------------
# Classification of one transaction
# ---------------------------------------------------------------------------
@dataclass
class Claim:
    tip_distribution_account: str
    claimant: str
    amount: int


def decode_claim_events(log_messages):
    """Every ClaimedEvent that the Tip Distribution program itself logged.

    The invoke stack is followed so that another program cannot fake a claim
    by logging the same bytes. Truncated logs lose the event: such a claim
    then shows up as an "other" balance increase in the summary.
    """
    claims, stack = [], []
    for line in log_messages or []:
        if not line.startswith("Program data: "):
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "Program":
                if parts[2] == "invoke":
                    stack.append(parts[1])
                elif (
                    parts[2] in ("success", "failed:")
                    and stack
                    and stack[-1] == parts[1]
                ):
                    stack.pop()
            continue
        if not stack or stack[-1] != TIP_DISTRIBUTION_PROGRAM:
            continue
        try:
            data = base64.b64decode(line[len("Program data: ") :], validate=True)
        except ValueError:
            continue
        if len(data) != CLAIMED_EVENT_LEN or data[:8] != CLAIMED_EVENT_DISCRIMINATOR:
            continue
        claims.append(
            Claim(
                tip_distribution_account=b58encode(data[8:40]),
                claimant=b58encode(data[72:104]),
                amount=int.from_bytes(data[104:112], "little"),
            )
        )
    return claims


def _account_keys(tx):
    return [
        k["pubkey"] if isinstance(k, dict) else k
        for k in tx["transaction"]["message"]["accountKeys"]
    ]


def _all_instructions(tx):
    yield from tx["transaction"]["message"].get("instructions", [])
    for group in tx["meta"].get("innerInstructions") or []:
        yield from group.get("instructions", [])


def owner_moves_into(tx, address):
    """Lamports moved into `address` by the owner's own operations.

    Split, MoveLamports/MoveStake and Merge need the stake authority of both
    accounts, so they move the owner's own SOL. A CreateAccount deposit counts
    only when the funder is an authority that the same transaction's
    Initialize sets: a third party can fund a new stake account too.
    Everything else that raised the balance (a Withdraw from another party's
    stake account, e.g. Validator Bonds, or a plain transfer) is left
    unexplained and reported as "other".
    """
    keys = _account_keys(tx)
    pre = tx["meta"]["preBalances"]
    owners = stake_authorities_set(tx, address)
    drained = lamport_sources(tx)
    moved = 0
    for ix in _all_instructions(tx):
        parsed = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else {}
        kind, info = parsed.get("type"), parsed.get("info") or {}
        if ix.get("programId") == STAKE_PROGRAM:
            if kind == "split" and info.get("newSplitAccount") == address:
                moved += int(info.get("lamports", 0))
            elif kind in ("moveLamports", "moveStake") and (
                info.get("destination") == address
            ):
                moved += int(info.get("lamports", 0))
            elif kind == "merge" and info.get("destination") == address:
                # Merge moves the whole source balance, known only from the
                # start of the transaction: if the source also pays out
                # elsewhere in it, the merged amount is unknown, so nothing
                # counts as explained and the increase goes to review.
                source = info.get("source")
                if source in keys and source not in drained:
                    moved += pre[keys.index(source)]
        elif ix.get("programId") == SYSTEM_PROGRAM and kind in (
            "createAccount",
            "createAccountWithSeed",
        ):
            if info.get("newAccount") == address and info.get("source") in owners:
                moved += int(info.get("lamports", 0))
    return moved


def lamport_sources(tx):
    """Accounts that pay lamports out in `tx`, other than by a merge."""
    paying = set()
    for ix in _all_instructions(tx):
        parsed = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else {}
        kind, info = parsed.get("type"), parsed.get("info") or {}
        if ix.get("programId") == STAKE_PROGRAM and kind in ("withdraw", "split"):
            paying.add(info.get("stakeAccount"))
        elif ix.get("programId") == STAKE_PROGRAM and kind in (
            "moveLamports",
            "moveStake",
        ):
            paying.add(info.get("source"))
        elif ix.get("programId") == SYSTEM_PROGRAM and kind in (
            "transfer",
            "transferWithSeed",
        ):
            paying.add(info.get("source"))
    paying.discard(None)
    return paying


def stake_authorities_set(tx, address):
    """Staker and withdrawer that an Initialize in `tx` gives `address`."""
    owners = set()
    for ix in _all_instructions(tx):
        if ix.get("programId") != STAKE_PROGRAM:
            continue
        parsed = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else {}
        info = parsed.get("info") or {}
        if info.get("stakeAccount") != address:
            continue
        if parsed.get("type") == "initialize":
            authorized = info.get("authorized") or {}
            owners.update((authorized.get("staker"), authorized.get("withdrawer")))
        elif parsed.get("type") == "initializeChecked":
            owners.update((info.get("staker"), info.get("withdrawer")))
    owners.discard(None)
    return owners


def program_label(tx):
    """The program(s) that drove a transaction, for the "other" report."""
    outer = [
        ix.get("programId")
        for ix in tx["transaction"]["message"].get("instructions", [])
        if ix.get("programId") not in PLUMBING_PROGRAMS
    ]
    labels = [KNOWN_PROGRAMS.get(pid, pid) for pid in dict.fromkeys(outer)]
    return "+".join(labels) or "stake_program"


@dataclass
class TxResult:
    """What one transaction did to one address."""

    delta: int  # balance change in lamports
    tip: int = 0  # lamports claimed by this address as Jito tips
    tip_accounts: list = field(default_factory=list)
    claims: list = field(default_factory=list)  # each Claim for this address
    kind: str = "none"  # jito_tip | stake_program | other | none
    label: str = ""  # for "other": the program(s) behind it
    other: int = 0  # for a tip: lamports beyond the tip that nothing explains


def classify(tx, address):
    keys = _account_keys(tx)
    meta = tx["meta"]
    if address not in keys or meta.get("err"):
        return TxResult(delta=0)
    index = keys.index(address)
    delta = meta["postBalances"][index] - meta["preBalances"][index]
    claims = [
        c for c in decode_claim_events(meta.get("logMessages")) if c.claimant == address
    ]
    if claims:
        tip = sum(c.amount for c in claims)
        # SOL that arrived in the same transaction as the claim, beyond the
        # tip and the owner's own moves, is reported as "other" as well.
        extra = delta - tip - owner_moves_into(tx, address)
        return TxResult(
            delta=delta,
            tip=tip,
            tip_accounts=[c.tip_distribution_account for c in claims],
            claims=claims,
            kind=INCOME_JITO_TIP,
            label=program_label(tx) if extra > 0 else "",
            other=max(extra, 0),
        )
    if delta <= 0:
        return TxResult(delta=delta)
    unexplained = delta - owner_moves_into(tx, address)
    if unexplained <= 0:
        return TxResult(delta=delta, kind="stake_program")
    # Only the part the owner's own moves do not explain is reported.
    return TxResult(delta=unexplained, kind="other", label=program_label(tx))


def match_tip_epoch(tip_account, voter, claim_epoch):
    """The epoch whose tips `tip_account` held, if it belongs to `voter`."""
    if not voter:
        return None
    for epoch in range(claim_epoch, max(claim_epoch - TIP_EPOCH_LOOKBACK, 0) - 1, -1):
        if tip_distribution_account(voter, epoch) == tip_account:
            return epoch
    return None


# ---------------------------------------------------------------------------
# Prices: sources fetch hourly points; the PriceBook keeps them in the
# PriceStore and plans which hours still need a call.
# ---------------------------------------------------------------------------
class PriceError(Exception):
    pass


def hour_of(ts):
    """The nearest whole UTC hour, as Unix seconds."""
    return (ts + 1800) // 3600 * 3600


def nearest_points(points, hours, max_gap=PRICE_MAX_GAP_SEC):
    """{hour: (usd, approximate)} from sorted (unix, usd) points, for each hour
    with a point within `max_gap` seconds."""
    times = [t for t, _ in points]
    found = {}
    for h in hours:
        i = bisect.bisect_left(times, h)
        near = [j for j in (i - 1, i) if 0 <= j < len(points)]
        if not near:
            continue
        j = min(near, key=lambda k: abs(times[k] - h))
        gap = abs(times[j] - h)
        if gap <= max_gap:
            found[h] = (points[j][1], gap > PRICE_APPROX_GAP_SEC)
    return found


class NoPrices:
    name = "none"
    span_sec = 0
    calls = 0

    def fetch(self, ticker, start, end):
        return {}


class WebgatorPrices:
    """Valigator's webgator-api /price/<ticker>/range (needs an API key; not
    public). One call returns every point in a window of span_sec (hourly
    points from CoinGecko), so a year needs about 5 calls."""

    name = "webgator"
    span_sec = WEBGATOR_WINDOW_SEC
    TICKERS = ("SOL", "JITOSOL", "2Z")

    def __init__(
        self, base_url, api_key, session=None, sleep=time.sleep, now=time.time
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.limiter = RateLimiter(WEBGATOR_REQUESTS_PER_SEC)
        self.sleep = sleep
        self.now = now
        self.calls = 0

    def fetch(self, ticker, start, end):
        if ticker not in self.TICKERS:
            raise PriceError(f"webgator has no history for {ticker}")
        now = int(self.now())
        # Ask for a margin each side, so the edge hours get their nearest
        # point. The API clamps a future `to` to now; `from` must be in the past.
        to = min(end + WEBGATOR_MARGIN_SEC, now)
        frm = min(start - WEBGATOR_MARGIN_SEC, to - 3600)
        self.calls += 1
        response = http_call(
            self.session,
            self.limiter,
            "GET",
            f"{self.base_url}/price/{ticker}/range",
            sleep=self.sleep,
            params={"from": str(frm), "to": str(to)},
            headers={"X-API-Key": self.api_key},
        )
        if response.status_code == 404:
            return {}  # no price in the range from any webgator source
        if response.status_code != 200:
            raise PriceError(
                f"webgator /price/{ticker}/range: HTTP {response.status_code}"
            )
        try:
            data = response.json().get("data") or {}
            raw = data.get("points")
            # No list is a bad answer, not "no data": stored as no data, the
            # hours would not be asked again for a week.
            if not isinstance(raw, list):
                raise PriceError(f"webgator /price/{ticker}/range: no points list")
            points = sorted((int(t), Decimal(str(p))) for t, p in raw)
            fallback = bool(data.get("approximate"))
        except (ValueError, AttributeError, TypeError, InvalidOperation) as exc:
            raise PriceError(
                f"webgator /price/{ticker}/range: bad answer ({exc.__class__.__name__})"
            ) from exc
        hours = range(start, end + 1, 3600)
        if not fallback:
            return nearest_points(points, hours)
        # epoch_detail points are ~2 days apart: allow webgator's own /at gap,
        # and mark every price approximate.
        found = nearest_points(points, hours, max_gap=EPOCH_FALLBACK_MAX_GAP_SEC)
        return {h: (usd, True) for h, (usd, _) in found.items()}


@dataclass
class BackfillResult:
    hours: int = 0  # final hours in the range
    had: int = 0  # already stored with a price
    filled: int = 0  # stored with a price by this run
    no_data: int = 0  # asked, and the source had no point near the hour
    left: int = 0  # still to ask (budget used up)
    calls: int = 0


class PriceBook:
    """Prices for reports and backfills, kept in a PriceStore.

    Only final hours (older than PRICE_FINAL_SEC) are stored, since a more
    recent hour can still get a nearer point. An hour the source had no point
    for is stored as "no data" with its fetch time, and is asked again only
    after PRICE_RETRY_MISSING_SEC.

    usd_at reads the store first, from any source. It calls the source only
    with fetch=True: prices are optional, and a report without them leaves
    the price columns empty until a `prices` backfill fills the store.
    """

    def __init__(self, store, source, now=time.time, offline=False, fetch=True):
        self.store = store
        self.source = source
        self.now = now
        self.offline = offline
        self.fetch = fetch
        self.unpriced_hours = set()

    def _final(self, hour, now):
        return hour + PRICE_FINAL_SEC <= now

    def _record(self, ticker, start, end, found, now):
        # A window fetched for one hour must not replace prices already stored
        # for its other hours (a sparse or empty answer would turn them into
        # "no data" or approximations). backfill only writes missing hours too.
        known = self.store.known(self.source.name, ticker, start, end)
        for h in range(start, end + 1, 3600):
            if not self._final(h, now) or known.get(h, (None,))[0] is not None:
                continue
            usd, approximate = found.get(h, (None, False))
            self.store.put(self.source.name, ticker, h, usd, approximate, now)
        self.store.commit()

    def usd_at(self, ticker, ts):
        """(usd, approximate) for the hour nearest `ts`; (None, False) if unknown."""
        hour, now = hour_of(ts), int(self.now())
        hit = self.store.get_any(ticker, hour, preferred=self.source.name)
        if hit:
            return hit
        tried = self.store.get(self.source.name, ticker, hour)
        if (
            self.offline
            or not self.fetch
            or (tried and now - tried[2] < PRICE_RETRY_MISSING_SEC)
        ):
            self.unpriced_hours.add(hour)
            return None, False
        # One call fills the source's whole window around the hour, so the
        # neighbouring hours are stored too.
        half = self.source.span_sec // 2 // 3600 * 3600
        start, end = hour - half, min(hour + half, max(hour, hour_of(now)))
        found = self.source.fetch(ticker, start, end)
        self._record(ticker, start, end, found, now)
        if hour not in found:
            self.unpriced_hours.add(hour)
        return found.get(hour, (None, False))

    def missing_hours(self, ticker, start, end):
        """Final hours in [start, end] to ask for: never stored, or "no data"
        that is old enough to ask again."""
        now = int(self.now())
        known = self.store.known(self.source.name, ticker, start, end)
        missing = []
        for h in range(start, end + 1, 3600):
            if not self._final(h, now):
                continue
            row = known.get(h)
            if row is None or (
                row[0] is None and now - row[1] >= PRICE_RETRY_MISSING_SEC
            ):
                missing.append(h)
        return missing

    def backfill(self, ticker, start, end, max_calls=None):
        """Fill missing hours in [start, end], at most `max_calls` calls. Each
        call's result is committed, so an interrupted run loses nothing and the
        next run starts where this one stopped."""
        now = int(self.now())
        start, end = hour_of(start), hour_of(end)
        result = BackfillResult()
        hours = [h for h in range(start, end + 1, 3600) if self._final(h, now)]
        result.hours = len(hours)
        missing = self.missing_hours(ticker, start, end)
        known = self.store.known(self.source.name, ticker, start, end)
        result.had = sum(1 for h in hours if known.get(h, (None,))[0] is not None)
        i = 0
        while i < len(missing):
            if max_calls is not None and result.calls >= max_calls:
                break
            first = missing[i]
            last = min(first + self.source.span_sec, end)
            # The batch: every missing hour up to `last`.
            j = bisect.bisect_right(missing, last, lo=i)
            batch_end = missing[j - 1]
            calls_before = self.source.calls
            found = self.source.fetch(ticker, first, batch_end)
            result.calls += self.source.calls - calls_before
            for h in missing[i:j]:
                if h in found:
                    result.filled += 1
                else:
                    result.no_data += 1
            # Store only the hours this batch asked for (not the stored ones
            # between them, which may have a price already).
            batch = set(missing[i:j])
            for h in batch:
                usd, approximate = found.get(h, (None, False))
                self.store.put(self.source.name, ticker, h, usd, approximate, now)
            self.store.commit()
            i = j
        result.left = len(missing) - i
        return result


def make_price_source(choice, env=os.environ):
    """The price source: webgator, or none. auto = webgator when its URL and
    key are set, else none (prices are optional: reports stay in SOL)."""
    webgator = (env.get("WEBGATOR_API_URL"), env.get("WEBGATOR_API_KEY"))
    if choice == "auto":
        choice = "webgator" if all(webgator) else "none"
    if choice == "none":
        return NoPrices()
    if not all(webgator):
        raise SystemExit(
            "--price-source webgator needs WEBGATOR_API_URL and WEBGATOR_API_KEY"
        )
    return WebgatorPrices(webgator[0], webgator[1])


def price_source_configured(choice, env=os.environ):
    """True when `choice` gives a price source that can fetch, so a hint never
    suggests a `prices` run that would be refused."""
    if choice == "none":
        return False
    return all((env.get("WEBGATOR_API_URL"), env.get("WEBGATOR_API_KEY")))


def unpriced_hint(hours, configured):
    """The summary line for hours with no stored price."""
    if not configured:
        return (
            f"# No price for {len(hours)} hour(s), so their USD values are empty."
            " No price source is set (WEBGATOR_API_URL and WEBGATOR_API_KEY)."
            " The SOL amounts are complete."
        )
    first = format_time(min(hours))[:10]
    last = format_time(max(hours))[:10]
    return (
        f"# Prices missing for {len(hours)} hour(s). To fill them:"
        f" stake_tips.py prices --from {first} --to {last}"
        " (or add --fetch-prices to this report)"
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@dataclass
class IncomeRow:
    address: str
    epoch: int
    tip_epoch: int
    slot: int
    block_time: int
    lamports: int
    income_type: str
    signature: str
    usd_price: Decimal = None
    price_approximate: bool = False


@dataclass
class Report:
    rows: list = field(default_factory=list)
    # IncomeRow with income_type "other:<program>"
    other_increases: list = field(default_factory=list)
    # (address, signature, tip, delta)
    tip_mismatches: list = field(default_factory=list)
    # (address, signature, slot): no block time, so a year cannot place them
    unknown_time: list = field(default_factory=list)


def format_sol(lamports):
    sign = "-" if lamports < 0 else ""
    whole, frac = divmod(abs(lamports), LAMPORTS_PER_SOL)
    return f"{sign}{whole}.{frac:09d}"


def format_time(ts):
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def usd_value(row, exact=False):
    """USD value of a row: rounded to 6 places for the CSV, or exact (for
    totals, so per-row rounding does not add up)."""
    if row.usd_price is None:
        return None
    value = Decimal(row.lamports) / LAMPORTS_PER_SOL * row.usd_price
    if exact:
        return value
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def collect_jito_tips(rpc, store, addresses, period, schedule, refresh=False):
    """Income source: Jito tip claims, read from the store (missing
    transactions are fetched). Other sources (e.g. inflation rewards via
    getInflationReward) can be added as functions with this signature."""
    report = Report()
    now = int(time.time())
    for address in addresses:
        voter = None
        for signature, slot, block_time in store.signatures(address):
            tx = None
            if block_time is None and period.year is not None:
                # The signature list can lack a time that the transaction
                # has: read it before deciding the year.
                tx = fetch_transaction(rpc, store, signature)
                block_time = tx.get("blockTime")
                if block_time is None:
                    # Last try: the slot's own time, kept in the store.
                    try:
                        block_time = rpc.call("getBlockTime", [slot])
                    except RpcError:
                        block_time = None
                    if block_time is not None:
                        store.set_block_time(signature, block_time)
                        store.commit()
                if block_time is None:
                    # Never dropped silently: the summary lists it.
                    report.unknown_time.append((address, signature, slot))
                    continue
            if not period.contains(slot, block_time, schedule):
                continue
            tx = tx or fetch_transaction(rpc, store, signature)
            result = classify(tx, address)
            # The transaction's own time, else the list's or getBlockTime's.
            when = tx.get("blockTime")
            when = block_time if when is None else when
            claim_epoch = schedule.epoch_of(slot)
            other = result.delta if result.kind == "other" else result.other
            if other > 0:
                report.other_increases.append(
                    IncomeRow(
                        address=address,
                        epoch=claim_epoch,
                        tip_epoch=None,
                        slot=slot,
                        block_time=when,
                        lamports=other,
                        income_type=f"other:{result.label}",
                        signature=signature,
                    )
                )
            if result.kind != INCOME_JITO_TIP:
                continue
            if result.tip != result.delta and not result.other:
                report.tip_mismatches.append(
                    (address, signature, result.tip, result.delta)
                )
            if voter is None:
                try:
                    voter = stake_voter(rpc, store, address, refresh, now) or ""
                except OfflineError:
                    voter = ""
            # One row per earned epoch: a transaction can claim several.
            by_epoch = {}
            for claim in result.claims:
                earned = match_tip_epoch(
                    claim.tip_distribution_account, voter, claim_epoch
                )
                by_epoch[earned] = by_epoch.get(earned, 0) + claim.amount
            for earned, lamports in sorted(
                by_epoch.items(), key=lambda item: (item[0] is None, item[0] or 0)
            ):
                report.rows.append(
                    IncomeRow(
                        address=address,
                        epoch=claim_epoch,
                        tip_epoch=earned,
                        slot=slot,
                        block_time=when,
                        lamports=lamports,
                        income_type=INCOME_JITO_TIP,
                        signature=signature,
                    )
                )
        store.commit()
    return report


INCOME_SOURCES = {INCOME_JITO_TIP: collect_jito_tips}

CSV_FIELDS = [
    "address",
    "epoch",
    "tip_epoch",
    "slot",
    "blocktime",
    "amount_sol",
    "income_type",
    "signature",
]
CSV_USD_FIELDS = ["sol_usd", "usd_value", "price_approximate"]


def add_prices(rows, book, ticker="SOL"):
    """Prices are optional: a source error during a report is a warning, the
    report goes on from the store alone, and those hours stay unpriced."""
    for row in rows:
        if row.block_time is None:
            continue
        try:
            row.usd_price, row.price_approximate = book.usd_at(ticker, row.block_time)
        except (PriceError, HttpError) as exc:
            warn(f"price fetch failed ({exc}); continuing with stored prices only")
            book.fetch = False
            row.usd_price, row.price_approximate = book.usd_at(ticker, row.block_time)


def write_csv(out, rows, usd):
    writer = csv.writer(out)
    writer.writerow(CSV_FIELDS + (CSV_USD_FIELDS if usd else []))
    for r in sorted(rows, key=lambda r: (r.address, r.slot)):
        line = [
            r.address,
            r.epoch,
            "" if r.tip_epoch is None else r.tip_epoch,
            r.slot,
            format_time(r.block_time),
            format_sol(r.lamports),
            r.income_type,
            r.signature,
        ]
        if usd:
            value = usd_value(r)
            line += [
                "" if r.usd_price is None else str(r.usd_price),
                "" if value is None else str(value),
                "yes" if r.price_approximate else "",
            ]
        writer.writerow(line)


def write_summary(out, report, addresses, period, usd):
    print(f"# Jito tips, {period.label()}", file=out)
    totals = {a: [0, Decimal(0), 0] for a in addresses}
    for r in report.rows:
        if r.income_type != INCOME_JITO_TIP:
            continue  # --all-increases rows are listed below, not totalled
        t = totals[r.address]
        t[0] += r.lamports
        t[2] += 1
        value = usd_value(r, exact=True)
        if value is not None:
            t[1] += value
    grand = sum(t[0] for t in totals.values())
    for address, (lamports, dollars, count) in sorted(totals.items()):
        extra = f"  ${dollars:,.2f}" if usd else ""
        print(
            f"{address}  {format_sol(lamports)} SOL  ({count} claims){extra}", file=out
        )
    extra = f"  ${sum(t[1] for t in totals.values()):,.2f}" if usd else ""
    print(f"TOTAL  {format_sol(grand)} SOL{extra}", file=out)
    missing = sum(
        1
        for r in report.rows
        if usd and r.income_type == INCOME_JITO_TIP and r.usd_price is None
    )
    if missing:
        print(
            f"# {missing} row(s) have no price; their USD value is not in the total",
            file=out,
        )
    if report.other_increases:
        print(
            "# Other transactions that increased a balance (not a Jito tip, not"
            " the owner's own Stake Program moves). Check them: some can be income.",
            file=out,
        )
        for r in report.other_increases:
            print(
                f"{r.address}  {r.signature}  +{format_sol(r.lamports)} SOL"
                f"  {r.income_type}",
                file=out,
            )
    if report.unknown_time:
        print(
            f"# WARNING: {len(report.unknown_time)} transaction(s) have no block"
            " time, so this year report cannot place them and can be incomplete:",
            file=out,
        )
        for address, signature, slot in report.unknown_time:
            print(f"{address}  {signature}  slot {slot}", file=out)
    for address, signature, tip, delta in report.tip_mismatches:
        print(
            f"# NOTE {signature}: tip {format_sol(tip)} SOL but the balance of"
            f" {address} changed by {format_sol(delta)} SOL",
            file=out,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def read_address_file(path):
    addresses = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if not is_pubkey(text):
            raise SystemExit(f"{path}:{number}: not a base58 address: {text!r}")
        addresses.append(text)
    return addresses


def parse_date(text):
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from exc
    return int(day.timestamp())


def parse_args(argv):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        default=None,
        help="where the stores live (default: $STAKE_TIPS_DATA, else the data/"
        " folder next to stake_tips.py)",
    )
    common.add_argument("-v", "--verbose", action="store_true")

    chain = argparse.ArgumentParser(add_help=False)
    chain.add_argument(
        "--rpc-url", help=f"default: $SOLANA_RPC_URL or {DEFAULT_RPC_URL}"
    )
    chain.add_argument(
        "--rpc-mode",
        choices=["auto", "standard", "helius"],
        default="auto",
        help="auto = Helius getTransactionsForAddress on a Helius URL",
    )
    chain.add_argument("--stakes", metavar="FILE", help="one stake account per line")
    chain.add_argument(
        "--wallets",
        metavar="FILE",
        help="one wallet per line: adds the open stake accounts it is the"
        " withdrawer or staker of",
    )
    chain.add_argument(
        "--refresh",
        action="store_true",
        help="walk the whole history again and redo wallet/delegation lookups",
    )

    pricing = argparse.ArgumentParser(add_help=False)
    pricing.add_argument(
        "--price-source",
        choices=["auto", "webgator", "none"],
        default="auto",
        help="auto = webgator if WEBGATOR_API_URL/KEY are set, else none",
    )

    p = argparse.ArgumentParser(
        description="Jito tip income for Solana stake accounts, kept in a local store."
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "sync",
        parents=[common, chain],
        help="fetch chain data into the store (no files: all tracked addresses)",
    )

    prices = sub.add_parser(
        "prices",
        parents=[common, pricing],
        help="backfill the hourly price store; resumable",
    )
    span = prices.add_mutually_exclusive_group(required=True)
    span.add_argument("--year", type=int, help="the UTC calendar year")
    span.add_argument("--from", dest="from_date", type=parse_date, help="YYYY-MM-DD")
    prices.add_argument(
        "--to", dest="to_date", type=parse_date, help="YYYY-MM-DD (default: now)"
    )
    prices.add_argument("--ticker", default="SOL", type=str.upper)
    prices.add_argument(
        "--max-calls", type=int, help="stop after this many API calls (resume later)"
    )

    report = sub.add_parser(
        "report", parents=[common, chain, pricing], help="CSV and summary"
    )
    when = report.add_mutually_exclusive_group()
    when.add_argument("--epoch", type=int, help="only claims that landed in this epoch")
    when.add_argument(
        "--year", type=int, help="only claims in this calendar year (UTC)"
    )
    report.add_argument(
        "--level",
        choices=["summary", "rows"],
        default="rows",
        help="rows = CSV rows + totals (default); summary = totals only",
    )
    report.add_argument(
        "--usd",
        action="store_true",
        help="add the SOL/USD price and USD value (rows and totals)",
    )
    report.add_argument(
        "--all-increases",
        action="store_true",
        help='also put the "other" balance increases in the CSV'
        " (income_type other:<program>)",
    )
    report.add_argument(
        "--fetch-prices",
        action="store_true",
        help="with --usd: fetch prices the store does not hold (default: store only)",
    )
    report.add_argument(
        "--offline",
        action="store_true",
        help="use only the store: no RPC or price calls",
    )
    report.add_argument("-o", "--output", metavar="FILE", help="CSV (default: stdout)")

    sub.add_parser("status", parents=[common, chain], help="what the stores hold")
    args = p.parse_args(argv)
    env_dir = os.environ.get("STAKE_TIPS_DATA")
    args.data_dir = args.data_dir or env_dir or str(DEFAULT_DATA_DIR)
    return args


def open_chain(args, offline=False):
    if offline:
        rpc = OfflineRpc()
        rpc.url = args.rpc_url or os.environ.get("SOLANA_RPC_URL")
    else:
        rpc = Rpc(
            args.rpc_url or os.environ.get("SOLANA_RPC_URL") or DEFAULT_RPC_URL,
            mode=args.rpc_mode,
        )
    return rpc, ChainStore(chain_store_path(args.data_dir, rpc))


def open_prices(args):
    directory = Path(args.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return PriceStore(directory / "prices.sqlite3")


def resolve_addresses(args, rpc, store, now, default_to_tracked):
    """Stake accounts from the files (which are then tracked), or every
    tracked address when no file is given and default_to_tracked is set."""
    stakes = read_address_file(args.stakes) if args.stakes else []
    wallets = read_address_file(args.wallets) if args.wallets else []
    if not stakes and not wallets and default_to_tracked:
        stakes, wallets = store.tracked("stake"), store.tracked("wallet")
    store.track(stakes, "stake", now)
    store.track(wallets, "wallet", now)
    store.commit()
    addresses = list(stakes)
    for wallet in wallets:
        found = wallet_stake_accounts(rpc, store, wallet, args.refresh, now)
        log(f"wallet {wallet}: {len(found)} stake account(s)")
        addresses.extend(found)
    addresses = list(dict.fromkeys(addresses))
    if not addresses:
        raise SystemExit("no stake accounts: give --stakes and/or --wallets")
    return addresses


def cmd_sync(args, now):
    rpc, store = open_chain(args)
    try:
        addresses = resolve_addresses(args, rpc, store, now, default_to_tracked=True)
        load_epoch_schedule(rpc, store)
        new = sync_all(rpc, store, addresses, args.refresh, now)
        print(
            f"synced {len(addresses)} stake account(s): {new} new signature(s),"
            f" {rpc.calls} RPC call(s)"
        )
    finally:
        store.close()


def cmd_prices(args, now):
    if args.year is not None:
        start, end = year_bounds(args.year)
        end -= 3600
    else:
        # --to is a whole day: up to its last hour.
        start = args.from_date
        end = args.to_date + 86400 - 3600 if args.to_date else now
    store = open_prices(args)
    try:
        source = make_price_source(args.price_source)
        if isinstance(source, NoPrices):
            # Without a source every hour would be stored as "no data".
            raise SystemExit(
                "prices needs a price source: set WEBGATOR_API_URL and"
                " WEBGATOR_API_KEY (in .env)"
            )
        book = PriceBook(store, source)
        result = book.backfill(args.ticker, start, end, args.max_calls)
        print(
            f"{args.ticker}/{source.name} {format_time(start)[:10]}..{format_time(end)[:10]}:"
            f" {result.hours} final hours; had {result.had}, filled {result.filled},"
            f" no data {result.no_data}, still to ask {result.left};"
            f" {result.calls} call(s)"
        )
        if result.left:
            print("Run the same command again to continue.")
    finally:
        store.close()


def cmd_report(args, now):
    if not args.stakes and not args.wallets:
        raise SystemExit("report: give --stakes FILE and/or --wallets FILE")
    if args.offline and args.refresh:
        # --refresh asks the chain again; offline it would only drop what the
        # store knows (e.g. delegations, so tip_epoch would go empty).
        raise SystemExit(
            "report: --refresh needs the network; it cannot go with --offline"
        )
    rpc, store = open_chain(args, offline=args.offline)
    prices = open_prices(args) if args.usd else None
    try:
        addresses = resolve_addresses(args, rpc, store, now, default_to_tracked=False)
        period = Period(epoch=args.epoch, year=args.year)
        schedule = load_epoch_schedule(rpc, store)
        for address in addresses:
            ensure_synced(rpc, store, address, schedule, period, args.refresh, now)
        report = Report()
        for collect in INCOME_SOURCES.values():
            part = collect(rpc, store, addresses, period, schedule, args.refresh)
            report.rows += part.rows
            report.other_increases += part.other_increases
            report.tip_mismatches += part.tip_mismatches
            report.unknown_time += part.unknown_time
        if args.all_increases:
            report.rows += report.other_increases
        book = None
        if args.usd:
            fetch = args.fetch_prices and not args.offline
            source = make_price_source(args.price_source if fetch else "none")
            if fetch and isinstance(source, NoPrices):
                warn("--fetch-prices: no price source is set; using stored prices only")
                fetch = False
            book = PriceBook(prices, source, offline=args.offline, fetch=fetch)
            add_prices(report.rows, book)
        summary_only = args.level == "summary"
        if not summary_only:
            if args.output:
                with open(args.output, "w", newline="") as out:
                    write_csv(out, report.rows, args.usd)
            else:
                write_csv(sys.stdout, report.rows, args.usd)
        summary_out = sys.stdout if summary_only else sys.stderr
        write_summary(summary_out, report, addresses, period, args.usd)
        if book and book.unpriced_hours:
            configured = price_source_configured(args.price_source)
            print(unpriced_hint(book.unpriced_hours, configured), file=summary_out)
        log(f"RPC calls this run: {rpc.calls}")
    finally:
        store.close()
        if prices:
            prices.close()


def cmd_status(args, now):
    try:
        _, store = open_chain(args, offline=True)
    except OfflineError:
        # Prices can be filled before any sync: still show them.
        print("chain store: none yet (run `sync`)")
    else:
        try:
            for name, count in store.counts().items():
                print(f"{name}: {count}")
        finally:
            store.close()
    prices = open_prices(args)
    try:
        rows = prices.coverage()
        print("prices (source ticker year: hours with a price / with no data):")
        for source, ticker, year, have, none in rows:
            print(f"  {source} {ticker} {year}: {have} / {none}")
        if not rows:
            print("  (empty)")
    finally:
        prices.close()


COMMANDS = {
    "sync": cmd_sync,
    "prices": cmd_prices,
    "report": cmd_report,
    "status": cmd_status,
}


def load_env_file(path=None):
    """Load settings from the .env file next to this script, if there is one.
    A variable already set in the environment is kept."""
    path = ENV_FILE if path is None else path
    if Path(path).is_file():
        load_dotenv(path, override=False)


def main(argv=None):
    global VERBOSE
    load_env_file()  # before parse_args: defaults read the environment
    args = parse_args(argv)
    VERBOSE = args.verbose
    try:
        COMMANDS[args.command](args, int(time.time()))
    except (RpcError, HttpError, PriceError, StoreError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    return 0


if __name__ == "__main__":
    sys.exit(main())
