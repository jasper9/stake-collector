"""stake_tips.py: the stake-account Jito tip store and report.

No test reaches the network: RPC and HTTP go to fakes, and transactions are
built here in the jsonParsed shape that getTransaction returns.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import struct
import tempfile
import unittest
import unittest.mock
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_stake_tips():
    spec = importlib.util.spec_from_file_location("stake_tips", REPO / "stake_tips.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


st = load_stake_tips()

# Real mainnet values, checked with solders' find_program_address (2026-11-13).
VOTE = "gaToR246dheK1DGAMEqxMdBJZwU4qFyt7DzhSwAHFWF"
TDA_1084 = "GcfpobKMNhaZL2C3n8GqjxHWnHpuQZC3GhTZ1FZ11cye"

STAKE = st.b58encode(bytes([7]) * 32)
OTHER_STAKE = st.b58encode(bytes([8]) * 32)
PAYER = st.b58encode(bytes([9]) * 32)
TDP = st.TIP_DISTRIBUTION_PROGRAM
ROUTER = "RouterBmuRBkPUbgEDMtdvTZ75GBdSREZR5uGUxxxpb"
BONDS = "vBoNdEvzMrSai7is21XgVYik65mqtaKXuSdMBJ1xkW4"
SYSTEM = "11111111111111111111111111111111"

MAINNET = st.EpochSchedule(432000, 0, 0)
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


def claim_event(tda, claimant, amount):
    data = (
        st.CLAIMED_EVENT_DISCRIMINATOR
        + st.b58decode(tda)
        + st.b58decode(PAYER)
        + st.b58decode(claimant)
        + struct.pack("<Q", amount)
    )
    return "Program data: " + base64.b64encode(data).decode()


def claim_logs(event_line, emitter=TDP):
    return [
        f"Program {ROUTER} invoke [1]",
        "Program log: Instruction: ClaimWithPayer",
        f"Program {emitter} invoke [2]",
        "Program log: Instruction: Claim",
        event_line,
        f"Program {emitter} success",
        f"Program {ROUTER} success",
    ]


def make_tx(
    delta, instructions, inner=(), logs=(), err=None, address=STAKE, block_time=1
):
    keys = [PAYER, address, OTHER_STAKE]
    pre = [10_000_000, 5_000_000, 5_000_000]
    post = list(pre)
    post[1] += delta
    return {
        "blockTime": block_time,
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": k} for k in keys],
                "instructions": list(instructions),
            }
        },
        "meta": {
            "err": err,
            "preBalances": pre,
            "postBalances": post,
            "innerInstructions": [{"index": 0, "instructions": list(inner)}]
            if inner
            else [],
            "logMessages": list(logs),
        },
    }


def tip_tx(amount=12345, tda=TDA_1084, claimant=STAKE, block_time=1794540000):
    return make_tx(
        amount,
        [{"programId": ROUTER, "accounts": []}],
        inner=[{"programId": TDP, "accounts": []}],
        logs=claim_logs(claim_event(tda, claimant, amount)),
        block_time=block_time,
    )


def stake_ix(kind, **info):
    return {"programId": st.STAKE_PROGRAM, "parsed": {"type": kind, "info": info}}


class TestBase58AndPda(unittest.TestCase):
    def test_round_trip_keeps_leading_zeros(self):
        for raw in (bytes(32), b"\0\0" + bytes(range(30)), bytes([255]) * 32):
            self.assertEqual(st.b58decode(st.b58encode(raw)), raw)
        self.assertEqual(st.b58encode(bytes(32)), "1" * 32)

    def test_is_pubkey(self):
        self.assertTrue(st.is_pubkey(VOTE))
        self.assertFalse(st.is_pubkey("0OIl"))
        self.assertFalse(st.is_pubkey("abc"))

    def test_tip_distribution_account_matches_mainnet(self):
        self.assertEqual(st.tip_distribution_account(VOTE, 1084), TDA_1084)

    def test_pda_is_off_curve_and_wallet_key_is_on_curve(self):
        self.assertFalse(st.is_on_curve(st.b58decode(TDA_1084)))
        self.assertTrue(st.is_on_curve(st.b58decode(VOTE)))

    @unittest.skipUnless(importlib.util.find_spec("solders"), "solders not installed")
    def test_matches_solders_when_installed(self):
        from solders.pubkey import Pubkey

        program = Pubkey.from_string(TDP)
        for epoch in range(0, 2000, 97):
            seeds = [st.TIP_DISTRIBUTION_SEED, bytes(32), epoch.to_bytes(8, "little")]
            want, bump = Pubkey.find_program_address(seeds, program)
            self.assertEqual(st.find_program_address(seeds, TDP), (str(want), bump))


class TestDecodeClaimEvents(unittest.TestCase):
    def test_decodes_event_from_tip_program(self):
        claims = st.decode_claim_events(claim_logs(claim_event(TDA_1084, STAKE, 42)))
        self.assertEqual(claims, [st.Claim(TDA_1084, STAKE, 42)])

    def test_ignores_same_bytes_from_another_program(self):
        logs = claim_logs(claim_event(TDA_1084, STAKE, 42), emitter=BONDS)
        self.assertEqual(st.decode_claim_events(logs), [])

    def test_ignores_other_events_and_bad_base64(self):
        logs = claim_logs("Program data: AAAA")
        logs.insert(4, "Program data: !!!not base64")
        self.assertEqual(st.decode_claim_events(logs), [])


class TestClassify(unittest.TestCase):
    def test_jito_tip(self):
        result = st.classify(tip_tx(12345), STAKE)
        self.assertEqual(result.kind, st.INCOME_JITO_TIP)
        self.assertEqual((result.tip, result.delta), (12345, 12345))
        self.assertEqual(result.tip_accounts, [TDA_1084])

    def test_extra_sol_with_a_claim_is_other(self):
        tx = tip_tx(12345)
        tx["meta"]["postBalances"][1] += 500  # 500 more lamports arrive
        tx["transaction"]["message"]["instructions"].append(
            {"programId": SYSTEM, "parsed": {"type": "transfer", "info": {}}}
        )
        result = st.classify(tx, STAKE)
        self.assertEqual(
            (result.kind, result.tip, result.other), ("jito_tip", 12345, 500)
        )
        self.assertEqual(result.label, "jito_router+system_transfer")

    def test_claim_for_another_claimant_is_not_our_tip(self):
        tx = tip_tx(12345, claimant=OTHER_STAKE)
        self.assertNotEqual(st.classify(tx, STAKE).kind, st.INCOME_JITO_TIP)

    def test_owner_stake_move_is_not_income(self):
        tx = make_tx(500, [stake_ix("merge", destination=STAKE, source=OTHER_STAKE)])
        self.assertEqual(st.classify(tx, STAKE).kind, "stake_program")

    def test_withdraw_from_another_stake_account_is_other(self):
        tx = make_tx(
            207844,
            [{"programId": BONDS, "accounts": []}],
            inner=[stake_ix("withdraw", stakeAccount=OTHER_STAKE, destination=STAKE)],
        )
        result = st.classify(tx, STAKE)
        self.assertEqual((result.kind, result.label), ("other", "validator_bonds"))

    def test_transfer_bundled_with_delegate_is_still_other(self):
        transfer = {
            "programId": SYSTEM,
            "parsed": {"type": "transfer", "info": {"destination": STAKE}},
        }
        tx = make_tx(
            2_000,
            [transfer, stake_ix("delegate", stakeAccount=STAKE, voteAccount=VOTE)],
        )
        result = st.classify(tx, STAKE)
        self.assertEqual((result.kind, result.delta), ("other", 2_000))

    def test_only_the_unexplained_part_is_other(self):
        split = stake_ix(
            "split", stakeAccount=OTHER_STAKE, newSplitAccount=STAKE, lamports=1_500
        )
        bonds = stake_ix("withdraw", stakeAccount=PAYER, destination=STAKE)
        tx = make_tx(1_800, [split, bonds])
        result = st.classify(tx, STAKE)
        self.assertEqual((result.kind, result.delta), ("other", 300))

    def test_owner_moves_explain_the_increase(self):
        cases = [
            stake_ix("merge", destination=STAKE, source=OTHER_STAKE),  # pre 5_000_000
            stake_ix("moveLamports", source=OTHER_STAKE, destination=STAKE, lamports=9),
        ]
        for ix, delta in zip(cases, (5_000_000, 9)):
            self.assertEqual(
                st.classify(make_tx(delta, [ix]), STAKE).kind, "stake_program"
            )

    def create_and_init(self, funder):
        create = {
            "programId": SYSTEM,
            "parsed": {
                "type": "createAccount",
                "info": {"source": funder, "newAccount": STAKE, "lamports": 9},
            },
        }
        init = stake_ix(
            "initialize",
            stakeAccount=STAKE,
            authorized={"staker": PAYER, "withdrawer": PAYER},
        )
        return make_tx(9, [create, init])

    def test_merge_from_a_source_that_also_pays_out_goes_to_review(self):
        merge = stake_ix("merge", destination=STAKE, source=OTHER_STAKE)
        drain = stake_ix(
            "withdraw", stakeAccount=OTHER_STAKE, destination=PAYER, lamports=1
        )
        result = st.classify(make_tx(5_000_000, [drain, merge]), STAKE)
        self.assertEqual((result.kind, result.delta), ("other", 5_000_000))

    def test_owner_funded_new_stake_account_is_not_income(self):
        self.assertEqual(
            st.classify(self.create_and_init(PAYER), STAKE).kind, "stake_program"
        )

    def test_third_party_funded_new_stake_account_is_other(self):
        result = st.classify(self.create_and_init(OTHER_STAKE), STAKE)
        self.assertEqual((result.kind, result.delta), ("other", 9))

    def test_plain_transfer_is_other(self):
        tx = make_tx(
            1_000,
            [{"programId": SYSTEM, "parsed": {"type": "transfer", "info": {}}}],
        )
        result = st.classify(tx, STAKE)
        self.assertEqual((result.kind, result.label), ("other", "system_transfer"))

    def test_decrease_and_failed_tx_are_ignored(self):
        self.assertEqual(st.classify(make_tx(-5, []), STAKE).kind, "none")
        failed = make_tx(5, [], err={"InstructionError": [0, "X"]})
        self.assertEqual(st.classify(failed, STAKE).kind, "none")

    def test_address_not_in_tx(self):
        self.assertEqual(st.classify(tip_tx(), OTHER_STAKE + "x").kind, "none")


class TestTipEpoch(unittest.TestCase):
    def test_claim_in_1085_paid_tips_of_1084(self):
        self.assertEqual(st.match_tip_epoch(TDA_1084, VOTE, 1085), 1084)

    def test_unknown_voter_gives_none(self):
        self.assertIsNone(st.match_tip_epoch(TDA_1084, None, 1085))
        self.assertIsNone(st.match_tip_epoch(TDA_1084, STAKE, 1085))


class TestEpochsAndPeriods(unittest.TestCase):
    def test_mainnet_schedule(self):
        self.assertEqual(MAINNET.epoch_of(468750000), 1085)
        self.assertEqual(MAINNET.last_slot(1085), 1086 * 432000 - 1)

    def test_warmup_schedule(self):
        warm = st.EpochSchedule(432000, 14, 524256)
        self.assertEqual(
            [warm.epoch_of(s) for s in (0, 31, 32, 95, 96)], [0, 0, 1, 1, 2]
        )
        self.assertEqual((warm.last_slot(0), warm.last_slot(1)), (31, 95))
        self.assertEqual(warm.epoch_of(524256), 14)
        self.assertEqual(warm.last_slot(13), 524255)

    def test_year_period(self):
        period = st.Period(year=2025)
        start, end = period.year_bounds()
        self.assertTrue(period.contains(0, start, MAINNET))
        self.assertFalse(period.contains(0, end, MAINNET))
        self.assertFalse(period.contains(0, None, MAINNET))
        self.assertFalse(period.covered_by(0, end - 1, MAINNET))
        self.assertTrue(period.covered_by(0, end, MAINNET))
        self.assertFalse(period.covered_by(0, None, MAINNET))  # time unknown

    def test_epoch_period(self):
        period = st.Period(epoch=1085)
        self.assertTrue(period.contains(468750000, None, MAINNET))
        self.assertFalse(period.covered_by(MAINNET.last_slot(1085) - 1, 0, MAINNET))
        self.assertTrue(period.covered_by(MAINNET.last_slot(1085), 0, MAINNET))
        self.assertFalse(st.Period().covered_by(10**12, 10**12, MAINNET))


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, timeout=None, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class TestHttp(unittest.TestCase):
    def test_rate_limiter_spaces_calls(self):
        fake = FakeClock()
        limiter = st.RateLimiter(2, clock=fake.clock, sleep=fake.sleep)
        for _ in range(3):
            limiter.wait()
        self.assertEqual(fake.slept, [0.5, 0.5])

    def test_retries_429_and_obeys_retry_after(self):
        fake = FakeClock()
        session = FakeSession(
            [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200, {})]
        )
        limiter = st.RateLimiter(1000, clock=fake.clock, sleep=fake.sleep)
        response = st.http_call(session, limiter, "GET", "u", sleep=fake.sleep)
        self.assertEqual(response.status_code, 200)
        self.assertIn(7, fake.slept)

    def test_gives_up_after_max_attempts(self):
        fake = FakeClock()
        session = FakeSession([FakeResponse(503)] * st.MAX_ATTEMPTS)
        limiter = st.RateLimiter(1000, clock=fake.clock, sleep=fake.sleep)
        with self.assertRaises(st.HttpError):
            st.http_call(session, limiter, "GET", "u", sleep=fake.sleep)
        self.assertEqual(len(session.requests), st.MAX_ATTEMPTS)

    def test_retry_after_as_http_date(self):
        response = FakeResponse(
            429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:30 GMT"}
        )
        now = 1445412480  # 07:28:00
        self.assertEqual(st._retry_after(response, 0, now=lambda: now), 31)
        self.assertEqual(st._retry_after(response, 0, now=lambda: now + 3600), 1)
        response.headers["Retry-After"] = "not a date"
        self.assertEqual(st._retry_after(response, 3), 8)

    def test_rpc_error_payload_raises(self):
        session = FakeSession([FakeResponse(200, {"error": {"message": "boom"}})])
        rpc = st.Rpc("http://rpc", session=session, limiter=st.RateLimiter(1000))
        with self.assertRaisesRegex(st.RpcError, "boom"):
            rpc.call("getSlot", [])


class FakeRpc:
    """Answers the RPC methods the tool uses, and logs the calls."""

    def __init__(
        self, signatures, transactions, voter=VOTE, finalized=10**9, mode="standard"
    ):
        self.signatures = signatures  # newest first, as the RPC returns them
        self.transactions = transactions
        self.voter = voter
        self.finalized = finalized
        self.mode = mode
        self.url = "http://fake-rpc"
        self.fail_helius = False
        self.chain_time = 1794600000  # 2026-11-13
        self.log = []

    @property
    def calls(self):
        return len(self.log)

    def call(self, method, params):
        self.log.append((method, params))
        if method == "getSlot":
            return self.finalized
        if method == "getBlockTime":
            return self.chain_time
        if method == "getGenesisHash":
            return MAINNET_GENESIS
        if method == "getEpochSchedule":
            return {
                "slotsPerEpoch": 432000,
                "firstNormalEpoch": 0,
                "firstNormalSlot": 0,
            }
        if method == "getSignaturesForAddress":
            options = params[1]
            rows = self.signatures
            if "until" in options:
                cut = [r["signature"] for r in rows].index(options["until"])
                rows = rows[:cut]
            return rows[: options["limit"]]
        if method == "getTransactionsForAddress":
            if self.fail_helius:
                raise st.RpcError("getTransactionsForAddress: Method not found")
            after = params[1]["filters"].get("slot", {}).get("gt", -1)
            data = []
            for row in reversed(self.signatures):  # oldest first
                if row["slot"] > after:
                    tx = dict(self.transactions[row["signature"]], slot=row["slot"])
                    tx["transaction"] = dict(
                        tx["transaction"], signatures=[row["signature"]]
                    )
                    data.append(tx)
            # Like Helius, the token is not null on the last page.
            return {"data": data, "paginationToken": "1:1"}
        if method == "getTransaction":
            return self.transactions[params[0]]
        if method == "getAccountInfo":
            delegation = {"stake": {"delegation": {"voter": self.voter}}}
            return {"value": {"data": {"parsed": {"info": delegation}}}}
        raise AssertionError(method)


def sig_row(signature, slot, block_time=1):
    return {"signature": signature, "slot": slot, "blockTime": block_time, "err": None}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = st.ChainStore(Path(self.tmp.name) / "chain.sqlite3")
        self.addCleanup(self.store.close)
        bonds = make_tx(
            300,
            [{"programId": BONDS, "accounts": []}],
            inner=[stake_ix("withdraw", stakeAccount=OTHER_STAKE, destination=STAKE)],
        )
        self.rpc = FakeRpc(
            [
                sig_row("tip-1085", 468750000),
                sig_row("bonds-1085", 468729000),
                sig_row("tip-1084", 468000000),
            ],
            {"tip-1085": tip_tx(), "bonds-1085": bonds, "tip-1084": tip_tx(5)},
        )

    def report(self, period, now=1_000_000, refresh=False, rpc=None):
        rpc = rpc or self.rpc
        st.ensure_synced(rpc, self.store, STAKE, MAINNET, period, refresh, now)
        return st.collect_jito_tips(rpc, self.store, [STAKE], period, MAINNET)


class TestReportSync(StoreTestCase):
    def test_epoch_report_rows_and_other_increases(self):
        report = self.report(st.Period(epoch=1085))
        self.assertEqual([r.signature for r in report.rows], ["tip-1085"])
        row = report.rows[0]
        self.assertEqual((row.epoch, row.tip_epoch, row.lamports), (1085, 1084, 12345))
        self.assertEqual(
            [r.income_type for r in report.other_increases], ["other:validator_bonds"]
        )
        fetched = [p[0] for m, p in self.rpc.log if m == "getTransaction"]
        self.assertNotIn("tip-1084", fetched)

    def test_year_filter_reads_the_time_from_the_transaction(self):
        # The signature list has no time; the transaction does (2026-11-13).
        for row in self.rpc.signatures:
            row["blockTime"] = None
        report = self.report(st.Period(year=2026))
        self.assertIn("tip-1085", [r.signature for r in report.rows])
        self.assertEqual(self.report(st.Period(year=2025)).rows, [])

    def test_year_filter_falls_back_to_the_slot_time(self):
        for row in self.rpc.signatures:
            row["blockTime"] = None
        for tx in self.rpc.transactions.values():
            tx["blockTime"] = None
        self.rpc.chain_time = 1794540000  # getBlockTime(slot): 2026-11-13
        report = self.report(st.Period(year=2026))
        self.assertEqual(len(report.rows), 2)
        self.assertEqual(report.unknown_time, [])
        # The rows carry the recovered time, so they get a date and a price.
        self.assertEqual({r.block_time for r in report.rows}, {1794540000})
        prices = st.PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.addCleanup(prices.close)
        prices.put("webgator", "SOL", st.hour_of(1794540000), Decimal(80), False, 1)
        st.add_prices(report.rows, st.PriceBook(prices, st.NoPrices(), fetch=False))
        self.assertEqual({r.usd_price for r in report.rows}, {Decimal(80)})
        # Stored: a second report asks nothing.
        calls = self.rpc.calls
        self.report(st.Period(year=2026), rpc=st.OfflineRpc())
        self.assertEqual(self.rpc.calls, calls)

    def test_year_report_lists_transactions_with_no_time(self):
        for row in self.rpc.signatures:
            row["blockTime"] = None
        for tx in self.rpc.transactions.values():
            tx["blockTime"] = None
        self.rpc.chain_time = None  # getBlockTime has no answer either
        report = self.report(st.Period(year=2026))
        self.assertEqual(report.rows, [])
        self.assertEqual(len(report.unknown_time), 3)
        out = io.StringIO()
        st.write_summary(out, report, [STAKE], st.Period(year=2026), usd=False)
        self.assertIn("3 transaction(s) have no block time", out.getvalue())

    def test_extra_sol_with_a_claim_is_listed(self):
        tx = self.rpc.transactions["tip-1085"]
        tx["meta"]["postBalances"][1] += 500
        report = self.report(st.Period(epoch=1085))
        self.assertEqual([r.lamports for r in report.rows], [12345])
        others = {r.signature: r.lamports for r in report.other_increases}
        self.assertEqual(others["tip-1085"], 500)
        self.assertEqual(report.tip_mismatches, [])

    def test_one_row_per_earned_epoch_in_a_transaction(self):
        tx = self.rpc.transactions["tip-1085"]
        tda_1083 = st.tip_distribution_account(VOTE, 1083)
        logs = tx["meta"]["logMessages"]
        logs.insert(4, claim_event(tda_1083, STAKE, 7))
        tx["meta"]["postBalances"][1] += 7
        report = self.report(st.Period(epoch=1085))
        rows = [(r.tip_epoch, r.lamports) for r in report.rows]
        self.assertEqual(rows, [(1083, 7), (1084, 12345)])
        self.assertEqual(report.tip_mismatches, [])

    def test_second_run_makes_no_rpc_calls(self):
        self.report(st.Period(epoch=1085))
        before = self.rpc.calls
        again = self.report(st.Period(epoch=1085), now=9_999_999)
        self.assertEqual(self.rpc.calls, before)
        self.assertEqual(len(again.rows), 1)

    def test_all_time_resync_asks_only_for_new_signatures(self):
        self.report(st.Period())
        self.rpc.signatures.insert(0, sig_row("tip-1086", 469000000))
        self.rpc.transactions["tip-1086"] = tip_tx(7)
        report = self.report(st.Period(), now=1_000_000 + st.SYNC_FRESH_SEC)
        sig_calls = [p for m, p in self.rpc.log if m == "getSignaturesForAddress"]
        self.assertEqual(sig_calls[-1][1]["until"], "tip-1085")
        self.assertEqual(len(report.rows), 3)

    def test_refresh_walks_the_whole_history_again(self):
        # First sync on a short-history RPC sees only the newest signature.
        full = list(self.rpc.signatures)
        self.rpc.signatures = full[:1]
        self.report(st.Period())
        # An archival RPC and --refresh: older claims are filled in.
        self.rpc.signatures = full
        report = self.report(st.Period(), now=1_000_001, refresh=True)
        sig_calls = [p for m, p in self.rpc.log if m == "getSignaturesForAddress"]
        self.assertNotIn("until", sig_calls[-1][1])
        self.assertEqual(
            sorted(r.signature for r in report.rows), ["tip-1084", "tip-1085"]
        )

    def test_recent_all_time_sync_is_reused(self):
        self.report(st.Period())
        before = self.rpc.calls
        self.report(st.Period(), now=1_000_000 + st.SYNC_FRESH_SEC - 1)
        self.assertEqual(self.rpc.calls, before)


class TestSyncAndOffline(StoreTestCase):
    def test_sync_stores_everything_then_offline_report_needs_no_rpc(self):
        new = st.sync_all(self.rpc, self.store, [STAKE], False, 1_000_000)
        self.assertEqual(new, 3)
        self.assertEqual(self.store.counts()["transactions"], 3)
        report = self.report(st.Period(), rpc=st.OfflineRpc())
        self.assertEqual(len(report.rows), 2)
        # tip-1085 matches; tip-1084 lands in epoch 1083 with the epoch-1084
        # TDA, which cannot be claimed before it is earned: no match.
        self.assertEqual(
            {r.signature: r.tip_epoch for r in report.rows},
            {"tip-1085": 1084, "tip-1084": None},
        )

    def test_offline_without_a_sync_says_so(self):
        with self.assertRaisesRegex(st.OfflineError, "never synced"):
            self.report(st.Period(), rpc=st.OfflineRpc())

    def test_interrupted_sync_keeps_fetched_transactions(self):
        st.sync_address(self.rpc, self.store, STAKE, False, 1)
        broken = self.rpc.transactions
        self.rpc.transactions = {"tip-1084": broken["tip-1084"]}  # oldest only
        with self.assertRaises(KeyError):
            st.sync_all(self.rpc, self.store, [STAKE], False, 2)
        self.store.close()  # no commit after the failure
        self.store = st.ChainStore(Path(self.tmp.name) / "chain.sqlite3")
        self.addCleanup(self.store.close)
        self.assertTrue(self.store.has_transaction("tip-1084"))

    def test_offline_warns_when_the_store_ends_before_the_period(self):
        self.rpc.finalized = 468729700  # synced part way through epoch 1085
        st.sync_all(self.rpc, self.store, [STAKE], False, 1_000_000)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.report(st.Period(epoch=1085), rpc=st.OfflineRpc())
        self.assertIn("can be incomplete", err.getvalue())
        # The cutoff shown is the chain's time, not the clock's.
        self.assertIn(st.format_time(self.rpc.chain_time), err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.report(st.Period(epoch=1084), rpc=st.OfflineRpc())
        self.assertEqual(err.getvalue(), "")

    def test_year_needs_the_chain_past_its_end_not_the_clock(self):
        # Synced on 2027-01-02 by the clock, but the RPC's finalized block
        # is still in 2026: 2026 is not complete yet.
        self.rpc.chain_time = 1798761600 - 3600
        st.sync_address(self.rpc, self.store, STAKE, False, 1798848000)
        state = self.store.sync_state(STAKE)
        self.assertFalse(st.Period(year=2026).covered_by(state[1], state[3], MAINNET))
        self.rpc.chain_time = 1798761600 + 60
        st.sync_address(self.rpc, self.store, STAKE, False, 1798848000 + 3600)
        state = self.store.sync_state(STAKE)
        self.assertTrue(st.Period(year=2026).covered_by(state[1], state[3], MAINNET))

    def test_old_store_gets_the_chain_time_column(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        db = st.sqlite3.connect(str(path))
        db.executescript(
            st.CHAIN_SCHEMA.replace(
                "synced_at INTEGER NOT NULL, chain_time INTEGER)",
                "synced_at INTEGER NOT NULL)",
            )
        )
        db.execute("INSERT INTO sync_state VALUES ('a', 's', 1, 2)")
        db.commit()
        db.close()
        old = st.ChainStore(path)
        self.addCleanup(old.close)
        self.assertEqual(old.sync_state("a"), ("s", 1, 2, None))

    def test_tracked_addresses_persist(self):
        self.store.track([STAKE], "stake", 1)
        self.store.track([STAKE, OTHER_STAKE], "stake", 2)
        self.store.track([PAYER], "wallet", 2)
        self.assertEqual(self.store.tracked("stake"), [STAKE, OTHER_STAKE])
        self.assertEqual(self.store.tracked("wallet"), [PAYER])

    def test_wallet_keeps_accounts_that_have_closed(self):
        class GpaRpc(FakeRpc):
            open_accounts = [STAKE, OTHER_STAKE]

            def call(self, method, params):
                if method == "getProgramAccounts":
                    self.log.append((method, params))
                    return [{"pubkey": a} for a in self.open_accounts]
                return super().call(method, params)

        rpc = GpaRpc([], {})
        found = st.wallet_stake_accounts(rpc, self.store, PAYER, False, 0)
        self.assertEqual(found, sorted([STAKE, OTHER_STAKE]))
        rpc.open_accounts = [STAKE]  # OTHER_STAKE closes
        later = st.WALLET_LOOKUP_TTL_SEC
        found = st.wallet_stake_accounts(rpc, self.store, PAYER, False, later)
        self.assertEqual(found, sorted([STAKE, OTHER_STAKE]))

    def test_refresh_reaches_the_delegation_lookup(self):
        self.rpc.voter = None  # not delegated at the first lookup
        report = self.report(st.Period(epoch=1085))
        self.assertIsNone(report.rows[0].tip_epoch)
        self.rpc.voter = VOTE
        st.ensure_synced(self.rpc, self.store, STAKE, MAINNET, st.Period(), True, 2)
        report = st.collect_jito_tips(
            self.rpc, self.store, [STAKE], st.Period(epoch=1085), MAINNET, True
        )
        self.assertEqual(report.rows[0].tip_epoch, 1084)

    def test_not_delegated_is_checked_again_after_a_day(self):
        self.rpc.voter = None
        self.assertIsNone(st.stake_voter(self.rpc, self.store, STAKE, False, 0))
        self.rpc.voter = VOTE
        self.assertIsNone(st.stake_voter(self.rpc, self.store, STAKE, False, 60))
        day = st.WALLET_LOOKUP_TTL_SEC
        self.assertEqual(st.stake_voter(self.rpc, self.store, STAKE, False, day), VOTE)
        # A known voter is kept (no RPC) until --refresh.
        calls = self.rpc.calls
        st.stake_voter(self.rpc, self.store, STAKE, False, 10 * day)
        self.assertEqual(self.rpc.calls, calls)

    def test_helius_sync_gets_full_transactions_in_one_call(self):
        self.rpc.mode = "helius"
        st.sync_all(self.rpc, self.store, [STAKE], False, 1_000_000)
        methods = [m for m, _ in self.rpc.log]
        self.assertEqual(methods.count("getTransactionsForAddress"), 1)
        self.assertNotIn("getTransaction", methods)
        self.assertNotIn("getSignaturesForAddress", methods)
        self.assertEqual(self.store.counts()["transactions"], 3)
        # The next sync asks only for slots after the last finalized sync.
        self.rpc.finalized = 2 * 10**9
        st.sync_address(self.rpc, self.store, STAKE, False, 1_000_100)
        options = self.rpc.log[-1][1][1]
        self.assertEqual(options["filters"]["slot"], {"gt": 10**9})
        self.assertEqual(options["maxSupportedTransactionVersion"], 1)
        report = self.report(st.Period(), rpc=st.OfflineRpc())
        self.assertEqual(len(report.rows), 2)

    def test_helius_answer_without_data_does_not_move_the_sync(self):
        self.rpc.mode = "helius"
        real = self.rpc.call

        def no_data(method, params):
            if method == "getTransactionsForAddress":
                return {"paginationToken": None}
            return real(method, params)

        self.rpc.call = no_data
        with self.assertRaisesRegex(st.RpcError, "no data list"):
            st.sync_address(self.rpc, self.store, STAKE, False, 1)
        self.assertIsNone(self.store.sync_state(STAKE))

    def test_auto_mode_falls_back_to_standard_rpc(self):
        self.rpc.mode = "auto"
        self.rpc.url = "https://mainnet.helius-rpc.com/?api-key=x"
        self.rpc.fail_helius = True
        with contextlib.redirect_stderr(io.StringIO()):
            st.sync_all(self.rpc, self.store, [STAKE], False, 1_000_000)
        self.assertEqual(self.rpc.mode, "standard")
        self.assertEqual(self.store.counts()["transactions"], 3)

    def test_explicit_helius_mode_does_not_hide_errors(self):
        self.rpc.mode = "helius"
        self.rpc.fail_helius = True
        with self.assertRaises(st.RpcError):
            st.sync_address(self.rpc, self.store, STAKE, False, 1_000_000)

    def test_use_helius(self):
        rpc = FakeRpc([], {}, mode="auto")
        for url, want in [
            ("https://mainnet.helius-rpc.com/?api-key=x", True),
            ("https://api.mainnet-beta.solana.com", False),
            ("https://helius-rpc.com.attacker.example", False),
        ]:
            rpc.url = url
            self.assertEqual(st.use_helius(rpc), want, url)
        rpc.mode = "standard"
        rpc.url = "https://mainnet.helius-rpc.com"
        self.assertFalse(st.use_helius(rpc))


HOUR = 1_700_000_000 // 3600 * 3600
LATER = HOUR + 400 * 86400  # long after HOUR: every hour near HOUR is final


class FakeSource:
    """A price source that answers from a dict and logs each call."""

    name = "fake"

    def __init__(self, prices, span_sec=0):
        self.prices = prices  # {hour: usd}
        self.span_sec = span_sec
        self.requests = []
        self.calls = 0

    def fetch(self, ticker, start, end):
        self.requests.append((ticker, start, end))
        self.calls += 1 if self.span_sec else (end - start) // 3600 + 1
        return {
            h: (Decimal(str(self.prices[h])), False)
            for h in range(start, end + 1, 3600)
            if h in self.prices
        }


class TestPrices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = st.PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.addCleanup(self.store.close)
        # The real limit (2/s) would make these tests wait.
        for name in ("WEBGATOR_REQUESTS_PER_SEC",):
            patcher = unittest.mock.patch.object(st, name, 1000)
            patcher.start()
            self.addCleanup(patcher.stop)

    def book(self, source, now=LATER, **kwargs):
        return st.PriceBook(self.store, source, now=lambda: now, **kwargs)

    def test_hour_of_rounds_to_nearest_hour(self):
        self.assertEqual(st.hour_of(3600 + 1799), 3600)
        self.assertEqual(st.hour_of(3600 + 1800), 7200)

    def test_nearest_points_gap_rules(self):
        points = [(HOUR, Decimal(1)), (HOUR + 4 * 3600, Decimal(2))]
        found = st.nearest_points(points, [HOUR, HOUR + 2 * 3600, HOUR + 11 * 3600])
        self.assertEqual(found[HOUR], (Decimal(1), False))
        self.assertEqual(found[HOUR + 2 * 3600], (Decimal(1), True))
        self.assertNotIn(HOUR + 11 * 3600, found)  # over 6 h from any point

    def test_store_only_by_default_prices_are_optional(self):
        source = FakeSource({HOUR: 90})
        book = self.book(source, fetch=False)
        self.assertEqual(book.usd_at("SOL", HOUR), (None, False))
        self.assertEqual(source.requests, [])
        self.assertEqual(book.unpriced_hours, {HOUR})
        # A later backfill fills the store; the same report then has the price.
        self.book(source).backfill("SOL", HOUR, HOUR)
        self.assertEqual(
            self.book(FakeSource({}), fetch=False).usd_at("SOL", HOUR),
            (Decimal("90"), False),
        )

    def test_store_answers_from_any_source_preferring_the_chosen_one(self):
        self.store.put("coingecko", "SOL", HOUR, Decimal(1), False, LATER)
        self.store.put("webgator", "SOL", HOUR, Decimal(2), False, LATER)
        self.assertEqual(self.store.get_any("SOL", HOUR, "webgator")[0], Decimal(2))
        self.assertEqual(self.store.get_any("SOL", HOUR, "coingecko")[0], Decimal(1))
        self.assertEqual(self.store.get_any("SOL", HOUR, "none")[0], Decimal(1))
        self.assertIsNone(self.store.get_any("2Z", HOUR))

    def test_fetch_on_demand_fills_the_window(self):
        prices = {HOUR + h * 3600: 100 + h for h in range(-3, 4)}
        source = FakeSource(prices, span_sec=6 * 3600)
        book = self.book(source)
        self.assertEqual(book.usd_at("SOL", HOUR), (Decimal("100"), False))
        self.assertEqual(book.usd_at("SOL", HOUR + 2 * 3600), (Decimal("102"), False))
        self.assertEqual(len(source.requests), 1)

    def test_on_demand_window_keeps_prices_already_stored(self):
        # HOUR + 3600 has a stored price; a window fetched for HOUR comes back
        # empty. The stored price must survive.
        self.store.put("fake", "SOL", HOUR + 3600, Decimal(42), False, LATER)
        source = FakeSource({}, span_sec=6 * 3600)
        self.assertEqual(self.book(source).usd_at("SOL", HOUR), (None, False))
        self.assertEqual(self.store.get("fake", "SOL", HOUR + 3600)[0], Decimal(42))
        self.assertIsNone(self.store.get("fake", "SOL", HOUR)[0])  # the missing one

    def test_recent_hour_is_not_stored(self):
        source = FakeSource({HOUR: 90})
        book = self.book(source, now=HOUR + 600)
        self.assertEqual(book.usd_at("SOL", HOUR), (Decimal("90"), False))
        self.assertIsNone(self.store.get("fake", "SOL", HOUR))
        book.usd_at("SOL", HOUR)
        self.assertEqual(len(source.requests), 2)

    def test_no_data_is_recorded_and_retried_after_a_week(self):
        source = FakeSource({})
        self.assertEqual(self.book(source).usd_at("SOL", HOUR), (None, False))
        stored = self.store.get("fake", "SOL", HOUR)
        self.assertEqual((stored[0], stored[2]), (None, LATER))
        self.book(source, now=LATER + 60).usd_at("SOL", HOUR)
        self.assertEqual(len(source.requests), 1)
        later = LATER + st.PRICE_RETRY_MISSING_SEC
        self.assertEqual(
            self.book(source, now=later).missing_hours("SOL", HOUR, HOUR), [HOUR]
        )
        self.book(source, now=later).usd_at("SOL", HOUR)
        self.assertEqual(len(source.requests), 2)

    def test_backfill_budget_and_resume(self):
        prices = {HOUR + h * 3600: 50 for h in range(10)}
        prices.pop(HOUR + 5 * 3600)  # one hour the source has no point for
        source = FakeSource(prices)  # one call per hour, like webgator
        end = HOUR + 9 * 3600
        first = self.book(source).backfill("SOL", HOUR, end, max_calls=4)
        self.assertEqual((first.calls, first.filled, first.left), (4, 4, 6))
        second = self.book(source).backfill("SOL", HOUR, end)
        self.assertEqual(
            (second.had, second.filled, second.no_data, second.left), (4, 5, 1, 0)
        )
        third = self.book(source).backfill("SOL", HOUR, end)
        self.assertEqual((third.calls, third.had), (0, 9))

    def test_backfill_batches_by_source_span(self):
        prices = {HOUR + h * 3600: 1 for h in range(48)}
        source = FakeSource(prices, span_sec=24 * 3600)
        result = self.book(source).backfill("SOL", HOUR, HOUR + 47 * 3600)
        self.assertEqual((result.calls, result.filled), (2, 48))
        self.assertEqual(source.requests[0], ("SOL", HOUR, HOUR + 24 * 3600))

    def webgator(self, *responses, now=LATER):
        session = FakeSession(list(responses))
        source = st.WebgatorPrices(
            "https://api/", "key", session=session, now=lambda: now
        )
        return source, session

    def test_webgator_fetch_uses_one_range_call(self):
        points = [[HOUR + h * 3600, 100 + h] for h in range(-1, 5)]
        body = {"data": {"points": points, "approximate": False}}
        source, session = self.webgator(FakeResponse(200, body))
        found = source.fetch("SOL", HOUR, HOUR + 3 * 3600)
        self.assertEqual(found[HOUR], (Decimal("100"), False))
        self.assertEqual(found[HOUR + 3 * 3600], (Decimal("103"), False))
        self.assertEqual(source.calls, 1)
        _, url, kwargs = session.requests[0]
        self.assertEqual(url, "https://api/price/SOL/range")
        margin = st.WEBGATOR_MARGIN_SEC
        self.assertEqual(
            kwargs["params"],
            {"from": str(HOUR - margin), "to": str(HOUR + 3 * 3600 + margin)},
        )
        self.assertEqual(kwargs["headers"], {"X-API-Key": "key"})

    def test_webgator_window_stays_inside_the_api_limit(self):
        # A whole window plus both margins is at most 80 days.
        self.assertEqual(
            st.WEBGATOR_WINDOW_SEC + 2 * st.WEBGATOR_MARGIN_SEC,
            st.WEBGATOR_RANGE_MAX_SEC,
        )
        # The margin covers the widest gap an hour may use.
        self.assertGreaterEqual(st.WEBGATOR_MARGIN_SEC, st.EPOCH_FALLBACK_MAX_GAP_SEC)
        body = {"data": {"points": [], "approximate": False}}
        source, session = self.webgator(FakeResponse(200, body))
        source.fetch("SOL", HOUR, HOUR + st.WEBGATOR_WINDOW_SEC)
        params = session.requests[0][2]["params"]
        self.assertLessEqual(
            int(params["to"]) - int(params["from"]), st.WEBGATOR_RANGE_MAX_SEC
        )

    def test_webgator_to_is_not_in_the_future(self):
        body = {"data": {"points": [[HOUR, 1]], "approximate": False}}
        now = HOUR + 2000
        source, session = self.webgator(FakeResponse(200, body), now=now)
        source.fetch("SOL", HOUR, HOUR + 3600)  # HOUR + 3600 is after now
        params = session.requests[0][2]["params"]
        self.assertEqual(params["to"], str(now))
        self.assertLess(int(params["from"]), now)

    def test_webgator_fallback_points_are_approximate_with_a_wider_gap(self):
        # epoch_detail points ~2 days apart: an hour 1 day away still gets one.
        points = [[HOUR - 86400, 90], [HOUR + 2 * 86400, 95]]
        body = {"data": {"points": points, "approximate": True}}
        source, _ = self.webgator(FakeResponse(200, body))
        found = source.fetch("SOL", HOUR, HOUR)
        self.assertEqual(found, {HOUR: (Decimal("90"), True)})

    def test_fallback_point_just_outside_a_window_still_prices_its_edge(self):
        # The nearest fallback point is 2 days before the window's first hour.
        class RangeSession(FakeSession):
            def request(self, method, url, timeout=None, **kwargs):
                self.requests.append((method, url, kwargs))
                f, t = int(kwargs["params"]["from"]), int(kwargs["params"]["to"])
                pts = [[HOUR - 2 * 86400, 90]]
                pts = [p for p in pts if f <= p[0] <= t]
                return FakeResponse(200, {"data": {"points": pts, "approximate": True}})

        source = st.WebgatorPrices(
            "https://api", "k", session=RangeSession([]), now=lambda: LATER
        )
        self.assertEqual(source.fetch("SOL", HOUR, HOUR), {HOUR: (Decimal("90"), True)})

    def test_webgator_404_is_no_data_and_bad_ticker_is_an_error(self):
        source, _ = self.webgator(FakeResponse(404, {}))
        self.assertEqual(source.fetch("SOL", HOUR, HOUR), {})
        with self.assertRaises(st.PriceError):
            source.fetch("BTC", HOUR, HOUR)
        source, _ = self.webgator(FakeResponse(400, {}))
        with self.assertRaisesRegex(st.PriceError, "HTTP 400"):
            source.fetch("SOL", HOUR, HOUR)

    def test_webgator_year_backfill_is_a_few_calls(self):
        year = [[HOUR + h * 3600, 1] for h in range(0, 366 * 24)]

        class RangeSession(FakeSession):
            def request(self, method, url, timeout=None, **kwargs):
                self.requests.append((method, url, kwargs))
                f, t = int(kwargs["params"]["from"]), int(kwargs["params"]["to"])
                pts = [p for p in year if f <= p[0] <= t]
                return FakeResponse(200, {"data": {"points": pts}})

        session = RangeSession([])
        source = st.WebgatorPrices(
            "https://api", "k", session=session, now=lambda: LATER
        )
        result = self.book(source).backfill("SOL", HOUR, HOUR + 364 * 86400)
        self.assertEqual(result.filled, 364 * 24 + 1)
        self.assertEqual(result.calls, 5)

    def test_price_error_in_a_report_leaves_the_row_unpriced(self):
        class Broken(FakeSource):
            def fetch(self, ticker, start, end):
                self.calls += 1
                raise st.PriceError("HTTP 401")

        self.store.put("fake", "SOL", HOUR + 3600, Decimal(7), False, LATER)
        book = self.book(Broken({}))
        rows = [
            st.IncomeRow(STAKE, 1, None, 1, ts, 1, "jito_tip", "s")
            for ts in (HOUR, HOUR + 3600)
        ]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            st.add_prices(rows, book)
        self.assertEqual([r.usd_price for r in rows], [None, Decimal(7)])
        self.assertFalse(book.fetch)
        self.assertEqual(book.source.calls, 1)
        self.assertIn("price fetch failed", err.getvalue())

    def test_bad_price_answers_are_price_errors(self):
        for body in (
            None,
            {"data": "x"},
            {"data": {}},
            {"data": {"points": None}},
            {"data": {"points": [["x", 1]]}},
            {"data": {"points": [[1, "abc"]]}},
        ):
            source, _ = self.webgator(FakeResponse(200, body))
            with self.assertRaises(st.PriceError, msg=body):
                source.fetch("SOL", HOUR, HOUR)

    def test_malformed_answer_stores_nothing(self):
        source, _ = self.webgator(FakeResponse(200, {"data": {"error": "x"}}))
        with self.assertRaises(st.PriceError):
            self.book(source).backfill("SOL", HOUR, HOUR)
        self.assertIsNone(self.store.get("webgator", "SOL", HOUR))

    def test_unpriced_hint_suggests_prices_only_with_a_source(self):
        env = {"WEBGATOR_API_URL": "https://api", "WEBGATOR_API_KEY": "k"}
        self.assertTrue(st.price_source_configured("auto", env))
        self.assertTrue(st.price_source_configured("webgator", env))
        self.assertFalse(st.price_source_configured("none", env))
        self.assertFalse(st.price_source_configured("auto", {}))
        self.assertFalse(st.price_source_configured("webgator", {}))
        hours = {1794538800}  # 2026-11-13
        hint = st.unpriced_hint(hours, configured=True)
        self.assertIn("prices --from 2026-11-13 --to 2026-11-13", hint)
        hint = st.unpriced_hint(hours, configured=False)
        self.assertIn("No price source is set", hint)
        self.assertNotIn("prices --from", hint)

    def test_auto_picks_webgator_only_with_url_and_key(self):
        env = {"WEBGATOR_API_URL": "https://api", "WEBGATOR_API_KEY": "k"}
        self.assertEqual(st.make_price_source("auto", env).name, "webgator")
        env = {"WEBGATOR_API_URL": "https://api"}
        self.assertEqual(st.make_price_source("auto", env).name, "none")
        self.assertEqual(st.make_price_source("none", {}).name, "none")
        with self.assertRaises(SystemExit):
            st.make_price_source("webgator", env)

    def test_coverage_by_year(self):
        self.store.put("webgator", "SOL", HOUR, Decimal(1), False, LATER)
        self.store.put("webgator", "SOL", HOUR + 3600, None, False, LATER)
        self.assertEqual(self.store.coverage(), [("webgator", "SOL", 2023, 1, 1)])

    def test_newer_schema_is_refused(self):
        self.store.set_meta("schema_version", st.SCHEMA_VERSION + 1)
        self.store.commit()
        with self.assertRaises(st.StoreError):
            st.PriceStore(self.store.path)


class TestOutput(unittest.TestCase):
    def row(self, lamports, income_type=st.INCOME_JITO_TIP, price=None):
        return st.IncomeRow(
            address=STAKE,
            epoch=1085,
            tip_epoch=1084 if income_type == st.INCOME_JITO_TIP else None,
            slot=468750000,
            block_time=1794540000,
            lamports=lamports,
            income_type=income_type,
            signature="sig",
            usd_price=price,
        )

    def test_formats(self):
        self.assertEqual(st.format_sol(12345), "0.000012345")
        self.assertEqual(st.format_sol(1_500_000_000), "1.500000000")
        self.assertEqual(st.format_time(1794540000), "2026-11-13 03:20:00")
        self.assertEqual(st.format_time(None), "")
        row = self.row(12345, price=Decimal("81.511648510944"))
        self.assertEqual(st.usd_value(row), Decimal("0.001006"))

    def test_csv_detail_columns(self):
        out = io.StringIO()
        st.write_csv(out, [self.row(12345, price=Decimal("80"))], usd=True)
        header, line = out.getvalue().splitlines()
        self.assertEqual(header.split(","), st.CSV_FIELDS + st.CSV_USD_FIELDS)
        self.assertEqual(
            line,
            f"{STAKE},1085,1084,468750000,2026-11-13 03:20:00,0.000012345,jito_tip,"
            "sig,80,0.000988,",
        )

    def test_usd_total_sums_unrounded_values(self):
        # Each row is $0.0000004 (rounds to $0.000000), 20,000 rows = $0.008.
        rows = [self.row(4, price=Decimal("100")) for _ in range(20000)]
        self.assertEqual(st.usd_value(rows[0]), Decimal("0.000000"))
        out = io.StringIO()
        st.write_summary(out, st.Report(rows=rows), [STAKE], st.Period(), usd=True)
        self.assertIn("TOTAL  0.000080000 SOL  $0.01", out.getvalue())

    def test_summary_totals_tips_only(self):
        other = self.row(300, income_type="other:validator_bonds")
        report = st.Report(rows=[self.row(12345), other], other_increases=[other])
        out = io.StringIO()
        st.write_summary(out, report, [STAKE], st.Period(epoch=1085), usd=False)
        text = out.getvalue()
        self.assertIn("TOTAL  0.000012345 SOL", text)
        self.assertIn("+0.000000300 SOL  other:validator_bonds", text)


ENV_KEYS = (
    "SOLANA_RPC_URL",
    "WEBGATOR_API_URL",
    "WEBGATOR_API_KEY",
    "STAKE_TIPS_DATA",
)


class TestCli(unittest.TestCase):
    def setUp(self):
        # main() loads the .env next to stake_tips.py: keep a developer's real .env and
        # exported settings out of these tests.
        env = unittest.mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        no_file = unittest.mock.patch.object(st, "ENV_FILE", Path("/nonexistent/.env"))
        no_file.start()
        self.addCleanup(no_file.stop)

    def test_env_file_is_loaded_and_the_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "SOLANA_RPC_URL=https://from-file\nWEBGATOR_API_KEY=file-key\n"
            )
            os.environ["WEBGATOR_API_KEY"] = "shell-key"
            st.load_env_file(env_file)
            self.assertEqual(os.environ["SOLANA_RPC_URL"], "https://from-file")
            self.assertEqual(os.environ["WEBGATOR_API_KEY"], "shell-key")
            st.load_env_file(Path(tmp) / "missing.env")  # no file: no error

    def test_main_reads_the_env_file_before_the_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(f"STAKE_TIPS_DATA={tmp}/store\n")
            with unittest.mock.patch.object(st, "ENV_FILE", env_file):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    st.main(["status"])
            self.assertIn("chain store: none yet", out.getvalue())
            self.assertTrue((Path(tmp) / "store" / "prices.sqlite3").exists())

    def test_address_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(f"# my stakes\n{STAKE}  # main\n\n{VOTE}\n")
        try:
            self.assertEqual(st.read_address_file(f.name), [STAKE, VOTE])
        finally:
            Path(f.name).unlink()
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("not-an-address\n")
        try:
            with self.assertRaises(SystemExit):
                st.read_address_file(f.name)
        finally:
            Path(f.name).unlink()

    def test_genesis_index_never_stores_the_rpc_url(self):
        class GenesisRpc:
            url = "https://rpc.example/?api-key=SECRET"
            calls = 0

            def call(self, method, params):
                self.calls += 1
                return MAINNET_GENESIS

        rpc = GenesisRpc()
        with tempfile.TemporaryDirectory() as tmp:
            path = st.chain_store_path(tmp, rpc)
            st.chain_store_path(tmp, rpc)
            index = (Path(tmp) / "clusters.json").read_text()
            # --offline finds the cluster from the index alone.
            offline = st.chain_store_path(tmp, st.OfflineRpc())
        self.assertEqual(path.name, f"{MAINNET_GENESIS}.sqlite3")
        self.assertEqual(offline, path)
        self.assertEqual(rpc.calls, 1)
        self.assertNotIn("SECRET", index)
        self.assertEqual(list(json.loads(index).values()), [MAINNET_GENESIS])

    def test_offline_unknown_url_is_refused(self):
        class GenesisRpc:
            url = "https://mainnet.example"

            def call(self, method, params):
                return MAINNET_GENESIS

        with tempfile.TemporaryDirectory() as tmp:
            st.chain_store_path(tmp, GenesisRpc())
            offline = st.OfflineRpc()
            offline.url = "https://testnet.example"  # never synced
            with self.assertRaisesRegex(st.OfflineError, "never synced"):
                st.chain_store_path(tmp, offline)
            offline.url = GenesisRpc.url  # known: fine
            self.assertEqual(
                st.chain_store_path(tmp, offline).name, f"{MAINNET_GENESIS}.sqlite3"
            )

    def test_genesis_that_is_not_a_hash_is_refused(self):
        class BadRpc:
            url = "https://rpc.example"

            def __init__(self, answer):
                self.answer = answer

            def call(self, method, params):
                return self.answer

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(st.RpcError):
                st.chain_store_path(tmp, BadRpc("../../escape"))
            # A tampered index entry is ignored and asked for again.
            key = st.hashlib.sha256(BadRpc.url.encode()).hexdigest()[:16]
            (Path(tmp) / "clusters.json").write_text(json.dumps({key: "../x"}))
            path = st.chain_store_path(tmp, BadRpc(MAINNET_GENESIS))
            self.assertEqual(path.parent, Path(tmp))
            self.assertEqual(path.name, f"{MAINNET_GENESIS}.sqlite3")

    def test_status_before_any_sync_shows_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            prices = st.PriceStore(Path(tmp) / "prices.sqlite3")
            prices.put("webgator", "SOL", HOUR, Decimal(1), False, LATER)
            prices.commit()
            prices.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                st.main(["status", "--data-dir", tmp])
        self.assertIn("chain store: none yet", out.getvalue())
        self.assertIn("webgator SOL 2023: 1 / 0", out.getvalue())

    def test_argument_rules(self):
        for argv in (
            [],
            ["report", "--stakes", "f", "--epoch", "1", "--year", "2025"],
            ["report", "--stakes", "f", "--detail"],
            ["report", "--stakes", "f", "--level", "all"],
            ["prices"],
            ["prices", "--year", "2025", "--from", "2025-01-01"],
            ["prices", "--from", "2025-13-01"],
        ):
            with (
                self.assertRaises(SystemExit, msg=argv),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                st.parse_args(argv)
        args = st.parse_args(["prices", "--from", "2025-01-01", "--ticker", "sol"])
        self.assertEqual((args.from_date, args.ticker), (1735689600, "SOL"))

    def test_summary_level_with_usd_prints_priced_totals_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeRpc([sig_row("tip-1085", 468750000)], {"tip-1085": tip_tx()})
            stakes = Path(tmp) / "stakes.txt"
            stakes.write_text(STAKE + "\n")
            prices = st.PriceStore(Path(tmp) / "prices.sqlite3")
            prices.put("webgator", "SOL", st.hour_of(1794540000), Decimal(80), False, 1)
            prices.commit()
            prices.close()
            out = io.StringIO()
            argv = ["report", "--data-dir", tmp, "--stakes", str(stakes)]
            patch = unittest.mock.patch.object(st, "Rpc", lambda url, mode: fake)
            with patch, contextlib.redirect_stdout(out):
                with contextlib.redirect_stderr(io.StringIO()):
                    st.main(argv + ["--epoch", "1085", "--level", "summary", "--usd"])
        text = out.getvalue()
        self.assertIn("TOTAL  0.000012345 SOL  $0.00", text)
        self.assertIn("(1 claims)", text)
        self.assertNotIn("signature", text)  # no CSV header

    def test_data_dir_default_and_overrides(self):
        args = st.parse_args(["status"])
        self.assertEqual(args.data_dir, str(st.DEFAULT_DATA_DIR))
        os.environ["STAKE_TIPS_DATA"] = "/tmp/elsewhere"
        self.assertEqual(st.parse_args(["status"]).data_dir, "/tmp/elsewhere")
        args = st.parse_args(["status", "--data-dir", "/tmp/flag"])
        self.assertEqual(args.data_dir, "/tmp/flag")

    def test_prices_without_a_source_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "needs a price source"):
                st.main(["prices", "--data-dir", tmp, "--year", "2025"])

    def test_offline_refresh_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            stakes = Path(tmp) / "stakes.txt"
            stakes.write_text(STAKE + "\n")
            argv = ["report", "--data-dir", tmp, "--stakes", str(stakes)]
            with self.assertRaisesRegex(SystemExit, "cannot go with --offline"):
                st.main(argv + ["--offline", "--refresh"])

    def test_report_without_prices_then_after_a_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeRpc([sig_row("tip-1085", 468750000)], {"tip-1085": tip_tx()})
            stakes = Path(tmp) / "stakes.txt"
            stakes.write_text(STAKE + "\n")
            out = Path(tmp) / "out.csv"
            argv = [
                "report",
                "--data-dir",
                tmp,
                "--stakes",
                str(stakes),
                "--epoch",
                "1085",
                "--usd",
                "-o",
                str(out),
            ]
            err = io.StringIO()
            with (
                unittest.mock.patch.object(st, "Rpc", lambda url, mode: fake),
                contextlib.redirect_stderr(err),
            ):
                st.main(argv)
            self.assertTrue(
                out.read_text().splitlines()[1].endswith("jito_tip,tip-1085,,,")
            )
            # No price source is set (setUp clears it): no `prices` command.
            self.assertIn("No price source is set", err.getvalue())
            self.assertNotIn("prices --from", err.getvalue())
            # Prices added later (here straight into the store), then offline.
            prices = st.PriceStore(Path(tmp) / "prices.sqlite3")
            prices.put("webgator", "SOL", st.hour_of(1794540000), Decimal(80), False, 1)
            prices.commit()
            prices.close()
            calls = fake.calls
            with contextlib.redirect_stderr(io.StringIO()):
                st.main(argv + ["--offline"])
            self.assertEqual(fake.calls, calls)
            self.assertIn(",80,0.000988,", out.read_text())


if __name__ == "__main__":
    unittest.main()
