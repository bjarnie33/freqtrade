# -*- coding: utf-8 -*-
import hashlib, io, json, math, os, random, sqlite3, sys, tempfile, threading, unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "shadow"))
import shadow_advisor as sa

NOW = datetime(2026, 10, 11, 18, 57, 0, tzinfo=timezone.utc)
KEY = "sk-ant-test-SECRET-123456"


def ft(dt):                      # eins og Freqtrade geymir daga: naive UTC med microsek.
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def cdate(dt):                   # eins og AuditedPaperStrategy: str(pandas Timestamp)
    return dt.strftime("%Y-%m-%d %H:%M:%S+00:00")


def payload(close=100.0, rsi=27.0, **over):
    p = {"open": close, "high": close, "low": close, "close": close, "volume": 1000.0, "return_1": -0.004, "return_12": -0.02, "volume_ratio": 1.8,
         "volatility_12": 0.0023, "sma_ratio": -0.013, "rsi_14": rsi, "macd_ratio": -0.0004}
    p.update(over)
    return json.dumps(p)


def make_trades_db(path, trades):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY, pair TEXT, is_open INTEGER, open_rate REAL, close_rate REAL, amount REAL, open_date TEXT, close_date TEXT, stake_amount REAL)")
    for t in trades:
        con.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)", (t["id"], t["pair"], t.get("is_open", 1), t["open_rate"], t.get("close_rate"), 1.0, ft(t["open_dt"]), None, 30.0))
    con.commit(); con.close()


def make_audit_db(path, candles):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE candles(pair TEXT, date TEXT, payload TEXT, PRIMARY KEY(pair, date))")
    for pair, dt, pl in candles:
        con.execute("INSERT OR IGNORE INTO candles VALUES(?,?,?)", (pair, cdate(dt), pl))
    con.commit(); con.close()


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class Env:
    def __init__(self, trades=(), candles=()):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.cfg = {"api_key": KEY, "model": "claude-haiku-5-5", "base_url": "http://127.0.0.1:1", "trades_db": os.path.join(d, "t.sqlite"), "audit_db": os.path.join(d, "a.sqlite"),
                    "state_db": os.path.join(d, "s.sqlite"), "poll": 0.0, "max_age_min": 10.0, "max_per_day": 300, "timeout": 5.0, "temperature": None, "retry_sleep_max": 0.0}
        make_trades_db(self.cfg["trades_db"], list(trades))
        make_audit_db(self.cfg["audit_db"], list(candles))
        self.state = sa.open_state(self.cfg["state_db"])
    def close(self):
        self.state.close(); self.tmp.cleanup()


def sig_trade(i, minutes_ago=2, pair="SOL/USDT", **kw):
    dt = NOW - timedelta(minutes=minutes_ago)
    t = {"id": i, "pair": pair, "open_rate": 100.0, "open_dt": dt}
    t.update(kw)
    return t


def sig_candle(trade, pair=None, minutes_before=5, **over):
    return (pair or trade["pair"], trade["open_dt"] - timedelta(minutes=minutes_before), payload(**over))


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
    def __call__(self, system, user):
        self.calls.append((system, user))
        r = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r, {"input_tokens": 400, "output_tokens": 40}, 123


APPROVE = '{"decision": "approve", "confidence": 0.7, "reason": "oversold with volume"}'
VETO = '{"decision": "veto", "confidence": 0.8, "reason": "falling knife"}'


# ------------------------------------------------------------------ 1. parsing
class ParseTests(unittest.TestCase):
    def test_valid_and_fenced_and_surrounded(self):
        self.assertEqual(sa.parse_decision(APPROVE)["decision"], "approve")
        self.assertEqual(sa.parse_decision("```json\n" + VETO + "\n```")["decision"], "veto")
        d = sa.parse_decision("Sure! " + APPROVE + " Hope that helps."); self.assertEqual(d["decision"], "approve")
    def test_rejects_garbage(self):
        for bad in ("", "nothing here", '{"decision": "maybe"}', '{"decision": ""}', "[1,2,3]", '{"confidence": 0.9}', "{not json}"):
            with self.assertRaises(ValueError, msg=bad): sa.parse_decision(bad)
    def test_confidence_is_clamped_and_nan_safe_and_reason_truncated(self):
        self.assertEqual(sa.parse_decision('{"decision":"veto","confidence":7}')["confidence"], 1.0)
        self.assertEqual(sa.parse_decision('{"decision":"veto","confidence":-3}')["confidence"], 0.0)
        self.assertEqual(sa.parse_decision('{"decision":"veto","confidence":"abc"}')["confidence"], 0.5)
        self.assertEqual(sa.parse_decision('{"decision":"veto","confidence":NaN}')["confidence"], 0.5)
        self.assertEqual(len(sa.parse_decision('{"decision":"veto","reason":"%s"}' % ("x" * 999))["reason"]), 240)
    def test_decision_is_case_insensitive(self):
        self.assertEqual(sa.parse_decision('{"decision":" APPROVE "}')["decision"], "approve")


# ------------------------------------------------------------------ 2. visar an leka
class FeatureTests(unittest.TestCase):
    def test_picks_the_signal_candle_and_never_a_later_one(self):
        t = sig_trade(1)
        env = Env([t], [("SOL/USDT", t["open_dt"] - timedelta(minutes=5), payload(rsi=27.0)),
                        ("SOL/USDT", t["open_dt"] + timedelta(minutes=5), payload(rsi=88.0)),       # EFTIR kaupin - ma ALDREI notast
                        ("SOL/USDT", t["open_dt"] + timedelta(seconds=30), payload(rsi=77.0))])
        ctx = sa.features_at(env.cfg["audit_db"], "SOL/USDT", t["open_dt"]); env.close()
        self.assertEqual(ctx["features"]["rsi_14"], 27.0)
    def test_candle_at_or_before_open_time_is_allowed_but_stale_ones_are_not(self):
        t = sig_trade(1)
        env = Env([t], [("SOL/USDT", t["open_dt"] - timedelta(minutes=40), payload())])
        self.assertIsNone(sa.features_at(env.cfg["audit_db"], "SOL/USDT", t["open_dt"])); env.close()
    def test_other_pairs_are_ignored(self):
        t = sig_trade(1); env = Env([t], [("ETH/USDT", t["open_dt"] - timedelta(minutes=5), payload())])
        self.assertIsNone(sa.features_at(env.cfg["audit_db"], "SOL/USDT", t["open_dt"])); env.close()
    def test_btc_context_comes_from_the_same_candle(self):
        t = sig_trade(1); d = t["open_dt"] - timedelta(minutes=5)
        env = Env([t], [("SOL/USDT", d, payload()), ("BTC/USDT", d, payload(return_12=-0.0031)), ("BTC/USDT", d + timedelta(minutes=5), payload(return_12=0.05))])
        ctx = sa.features_at(env.cfg["audit_db"], "SOL/USDT", t["open_dt"]); env.close()
        self.assertAlmostEqual(ctx["btc_return_12"], -0.0031)
    def test_missing_or_non_finite_features_give_none(self):
        t = sig_trade(1); d = t["open_dt"] - timedelta(minutes=5)
        for bad in ({"rsi_14": None}, {"rsi_14": float("nan")}, {"volume_ratio": "x"}):
            p = json.loads(payload()); p.update(bad); env = Env([t], [("SOL/USDT", d, json.dumps(p, allow_nan=True))])
            self.assertIsNone(sa.features_at(env.cfg["audit_db"], "SOL/USDT", t["open_dt"]), bad); env.close()
        p = json.loads(payload()); del p["macd_ratio"]; env = Env([t], [("SOL/USDT", d, json.dumps(p))])
        self.assertIsNone(sa.features_at(env.cfg["audit_db"], "SOL/USDT", t["open_dt"])); env.close()


class PromptTests(unittest.TestCase):
    def build(self):
        return sa.build_prompt("SOL/USDT", NOW, {k: 1.5 for k in sa.FEATURE_KEYS}, -0.003, 37)
    def test_prompt_contains_only_entry_time_information(self):
        text = self.build().lower()
        for word in ("profit", "exit", "stop", "outcome", "close_rate", "sold", "sell", "result", "pnl", "won", "lost"):
            self.assertNotIn(word, text, word)
        self.assertIn("sol/usdt", text); self.assertIn("37", text)
    def test_prompt_lists_exactly_the_whitelisted_indicators(self):
        lines = [l for l in self.build().split("\n") if l.startswith("  ") and "(" in l and "btc" not in l and "positions" not in l]
        self.assertEqual([l.strip().split(" ")[0] for l in lines], sa.FEATURE_KEYS)
    def test_unknown_btc_is_stated_not_invented(self):
        self.assertIn("unknown", sa.build_prompt("SOL/USDT", NOW, {k: 1.0 for k in sa.FEATURE_KEYS}, None, 0))


# ------------------------------------------------------------------ 3. nyjar akvardanir, ekkert baksetning, engin tvitekning
class ReadNewTradesTests(unittest.TestCase):
    def test_only_recent_and_undecided_trades_are_returned_no_backfill(self):
        env = Env([sig_trade(1, 2), sig_trade(2, 9), sig_trade(3, 30), sig_trade(4, 600), sig_trade(5, 3)])
        env.state.execute("INSERT INTO shadow_decisions(trade_id,pair,open_date,decided_at) VALUES(5,'X','d','d')"); env.state.commit()
        trades, open_n = sa.read_new_trades(env.cfg["trades_db"], env.state, NOW, 10)
        self.assertEqual(sorted(t["id"] for t in trades), [1, 2]); self.assertEqual(open_n, 5); env.close()


class DecideOnceTests(unittest.TestCase):
    def setUp(self):
        self.t1, self.t2 = sig_trade(1, 2), sig_trade(2, 3, pair="ETH/USDT")
        self.env = Env([self.t1, self.t2], [sig_candle(self.t1), sig_candle(self.t2), ("BTC/USDT", self.t1["open_dt"] - timedelta(minutes=5), payload(return_12=-0.002))])
    def tearDown(self): self.env.close()
    def rows(self): return {r[0]: r for r in self.env.state.execute("SELECT trade_id, decision, confidence, reason, model, prompt_version, features, input_tokens, error FROM shadow_decisions")}
    def test_decisions_are_stored_with_metadata(self):
        llm = FakeLLM([APPROVE, VETO]); made, errors = sa.decide_once(self.env.cfg, self.env.state, NOW, llm)
        self.assertEqual((made, errors), (2, 0)); r = self.rows()
        self.assertEqual({r[1][1], r[2][1]}, {"approve", "veto"}); self.assertEqual(r[1][4], "claude-haiku-5-5"); self.assertEqual(r[1][5], sa.PROMPT_VERSION)
        f = json.loads(r[1][6]); self.assertEqual(f["rsi_14"], 27.0); self.assertEqual(f["open_positions"], 2); self.assertEqual(r[1][7], 400)
    def test_no_duplicates_and_no_repeat_llm_calls(self):
        llm = FakeLLM([APPROVE]); sa.decide_once(self.env.cfg, self.env.state, NOW, llm); n = len(llm.calls)
        sa.decide_once(self.env.cfg, self.env.state, NOW + timedelta(seconds=30), llm)
        self.assertEqual(len(llm.calls), n); self.assertEqual(len(self.rows()), 2)
    def test_system_prompt_and_user_prompt_are_what_we_pre_registered(self):
        llm = FakeLLM([APPROVE]); sa.decide_once(self.env.cfg, self.env.state, NOW, llm)
        self.assertTrue(all(c[0] == sa.SYSTEM_PROMPT for c in llm.calls)); self.assertIn("rsi_14", llm.calls[0][1])
    def test_missing_features_skip_the_llm_and_are_recorded(self):
        t3 = sig_trade(3, 2, pair="NOFEAT/USDT"); env = Env([t3], []); llm = FakeLLM([APPROVE])
        made, errors = sa.decide_once(env.cfg, env.state, NOW, llm)
        row = env.state.execute("SELECT decision, error, model FROM shadow_decisions WHERE trade_id=3").fetchone(); env.close()
        self.assertEqual((made, len(llm.calls)), (0, 0)); self.assertEqual(row, (None, "no_features", None))
    def test_llm_failure_is_recorded_and_other_trades_continue_and_key_is_redacted(self):
        llm = FakeLLM([RuntimeError("boom " + KEY), APPROVE]); made, errors = sa.decide_once(self.env.cfg, self.env.state, NOW, llm)
        self.assertEqual((made, errors), (1, 1)); rows = self.env.state.execute("SELECT decision, error FROM shadow_decisions ORDER BY trade_id").fetchall()
        err = [r[1] for r in rows if r[1]][0]; self.assertNotIn(KEY, err); self.assertIn("***", err)
        buf = io.StringIO()
        with redirect_stdout(buf):
            env2 = Env([sig_trade(9, 2)], [sig_candle(sig_trade(9, 2))]); sa.decide_once(env2.cfg, env2.state, NOW, FakeLLM([RuntimeError("x " + KEY)])); env2.close()
        self.assertNotIn(KEY, buf.getvalue())
    def test_bad_output_is_recorded(self):
        made, errors = sa.decide_once(self.env.cfg, self.env.state, NOW, FakeLLM(["I think you should buy!"]))
        self.assertEqual((made, errors), (0, 2)); self.assertTrue(all(r[8].startswith("bad_output") for r in self.rows().values()))
    def test_auth_failure_is_recorded_then_raised(self):
        with self.assertRaises(sa.LLMError): sa.decide_once(self.env.cfg, self.env.state, NOW, FakeLLM([sa.LLMError(401, "invalid x-api-key")]))
        self.assertEqual(len(self.rows()), 1)
    def test_daily_cap_stops_calls(self):
        trades = [sig_trade(i, 2, pair="P%d/USDT" % i) for i in range(1, 6)]
        env = Env(trades, [sig_candle(t) for t in trades]); env.cfg["max_per_day"] = 2; llm = FakeLLM([APPROVE])
        buf = io.StringIO()
        with redirect_stdout(buf): made, _ = sa.decide_once(env.cfg, env.state, NOW, llm)
        self.assertEqual((made, len(llm.calls)), (2, 2)); self.assertIn("hamark", buf.getvalue()); env.close()
    def test_databases_of_the_bot_are_never_modified_and_cannot_be_written(self):
        before = (sha(self.env.cfg["trades_db"]), sha(self.env.cfg["audit_db"]))
        sa.decide_once(self.env.cfg, self.env.state, NOW, FakeLLM([APPROVE]))
        self.assertEqual(before, (sha(self.env.cfg["trades_db"]), sha(self.env.cfg["audit_db"])))
        con = sa.ro_connect(self.env.cfg["trades_db"])
        with self.assertRaises(sqlite3.OperationalError): con.execute("INSERT INTO trades(id,pair) VALUES(99,'x')")
        con.close()


# ------------------------------------------------------------------ 4. HTTP-lagid
class FakeAPI:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        outer = self
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length", 0)); body = self.rfile.read(n).decode()
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(body)})
                status, data, hdr = outer.responses[min(len(outer.requests) - 1, len(outer.responses) - 1)]
                raw = (data if isinstance(data, str) else json.dumps(data)).encode()
                self.send_response(status)
                for k, v in hdr.items(): self.send_header(k, v)
                self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def log_message(self, *a): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    @property
    def url(self): return "http://127.0.0.1:%d" % self.server.server_port
    def stop(self): self.server.shutdown(); self.server.server_close()


OK = (200, {"content": [{"type": "text", "text": APPROVE}], "usage": {"input_tokens": 410, "output_tokens": 33}}, {})


class HttpTests(unittest.TestCase):
    def cfg(self, api, **kw):
        c = {"api_key": KEY, "model": "claude-haiku-5-5", "base_url": api.url, "timeout": 5.0, "temperature": None, "retry_sleep_max": 0.0}; c.update(kw); return c
    def test_request_shape_matches_the_messages_api(self):
        api = FakeAPI([OK]); text, usage, ms = sa.make_llm(self.cfg(api))("SYS", "USER"); api.stop()
        r = api.requests[0]; self.assertEqual(r["path"], "/v1/messages")
        self.assertEqual(r["headers"]["x-api-key"], KEY); self.assertEqual(r["headers"]["anthropic-version"], "2023-06-01"); self.assertEqual(r["headers"]["content-type"], "application/json")
        b = r["body"]; self.assertEqual(b["model"], "claude-haiku-5-5"); self.assertEqual(b["system"], "SYS"); self.assertEqual(b["messages"], [{"role": "user", "content": "USER"}])
        self.assertIsInstance(b["max_tokens"], int); self.assertNotIn("temperature", b)
        self.assertEqual(sa.parse_decision(text)["decision"], "approve"); self.assertEqual(usage["input_tokens"], 410)
    def test_temperature_is_sent_only_when_configured(self):
        api = FakeAPI([OK]); sa.make_llm(self.cfg(api, temperature=0.0))("S", "U"); api.stop(); self.assertEqual(api.requests[0]["body"]["temperature"], 0.0)
    def test_rate_limit_is_retried_then_succeeds(self):
        api = FakeAPI([(429, {"error": {"message": "slow down"}}, {"retry-after": "0"}), OK]); text, _, _ = sa.make_llm(self.cfg(api))("S", "U"); api.stop()
        self.assertEqual(len(api.requests), 2); self.assertIn("approve", text)
    def test_server_errors_are_retried_three_times_then_raised(self):
        api = FakeAPI([(500, "oops", {})]); 
        with self.assertRaises(sa.LLMError) as cm: sa.make_llm(self.cfg(api))("S", "U")
        api.stop(); self.assertEqual(len(api.requests), 3); self.assertEqual(cm.exception.status, 500)
    def test_auth_and_bad_request_errors_are_not_retried_and_never_leak_the_key(self):
        for status in (401, 400, 404):
            api = FakeAPI([(status, {"error": {"message": "bad key " + KEY}}, {})])
            with self.assertRaises(sa.LLMError) as cm: sa.make_llm(self.cfg(api))("S", "U")
            api.stop(); self.assertEqual(len(api.requests), 1, status); self.assertNotIn(KEY, str(cm.exception)); self.assertEqual(cm.exception.status, status)
    def test_connection_error_is_an_llm_error(self):
        c = {"api_key": KEY, "model": "m", "base_url": "http://127.0.0.1:1", "timeout": 1.0, "temperature": None, "retry_sleep_max": 0.0}
        with self.assertRaises(sa.LLMError): sa.make_llm(c)("S", "U")
    def test_non_text_blocks_are_ignored(self):
        api = FakeAPI([(200, {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": VETO}], "usage": {}}, {})])
        text, _, _ = sa.make_llm(self.cfg(api))("S", "U"); api.stop(); self.assertEqual(sa.parse_decision(text)["decision"], "veto")


class LoopTests(unittest.TestCase):
    def test_loop_survives_errors_and_backs_off_after_an_auth_failure(self):
        t = sig_trade(1, 2); env = Env([t], [sig_candle(t)]); sleeps = []
        buf = io.StringIO()
        with redirect_stdout(buf):
            sa.run_forever(env.cfg, llm=FakeLLM([sa.LLMError(401, "bad key")]), max_loops=2, sleep=sleeps.append, clock=lambda: NOW)
        self.assertIn(600, sleeps); self.assertIn("LYKILL HAFNAD", buf.getvalue()); self.assertNotIn(KEY, buf.getvalue()); env.close()
    def test_loop_survives_a_missing_database(self):
        env = Env(); env.cfg["trades_db"] = "/nonexistent/x.sqlite"; buf = io.StringIO()
        with redirect_stdout(buf): sa.run_forever(env.cfg, llm=FakeLLM([APPROVE]), max_loops=3, sleep=lambda s: None, clock=lambda: NOW)
        self.assertIn("gagnagrunnsvilla", buf.getvalue()); env.close()
    def test_loop_decides_new_trades_as_they_appear(self):
        t = sig_trade(1, 2); env = Env([t], [sig_candle(t)]); llm = FakeLLM([APPROVE])
        with redirect_stdout(io.StringIO()): sa.run_forever(env.cfg, llm=llm, max_loops=3, sleep=lambda s: None, clock=lambda: NOW)
        self.assertEqual(len(llm.calls), 1); env.close()


# ------------------------------------------------------------------ 5. tolfraedin
def planted(n, gap, seed=1, noise=0.01):
    rng = random.Random(seed); rows = []
    for i in range(n):
        ok = i % 2 == 0
        rows.append((rng.gauss(gap / 2 if ok else -gap / 2, noise), ok))
    return rows


class TotalsTests(unittest.TestCase):
    def test_usdt_formula_matches_the_dashboard_definition(self):
        # kaup 30 USDT @100, sala @110, gjald 0,5%: proceeds 33*(0.995) - cost 30*(1.005)
        ret = sa.net_return(100, 110, 0.005); self.assertAlmostEqual(sa.usdt(ret, 0.005, 0.0, 30.0), 33 * 0.995 - 30 * 1.005)
        ret = sa.net_return(100, 110, 0.005, 0.0015); self.assertAlmostEqual(sa.usdt(ret, 0.005, 0.0015, 30.0), 30 * 1.1 * 0.9985 * 0.995 - 30 * 1.0015 * 1.005)
    def test_report_totals_equal_manual_sums_and_change_equals_minus_vetoed(self):
        trades, candles, dec = build_report_env(n=120); env = Env(trades, candles); fill_state(env, dec)
        buf = io.StringIO()
        with redirect_stdout(buf): res = sa.report(env.cfg, out=print, now=NOW, B=100, stake=50.0)
        rows, _ = sa.load_outcomes(env.cfg, env.state, NOW, prompt_version=sa.PROMPT_VERSION); env.close()
        for label, fee in sa.FEES.items():
            all_ = sum(sa.usdt(sa.net_return(r["entry"], r["exit"], fee), fee, 0.0, 50.0) for r in rows)
            veto = sum(sa.usdt(sa.net_return(r["entry"], r["exit"], fee), fee, 0.0, 50.0) for r in rows if not r["approved"])
            self.assertAlmostEqual(res[label]["total_all"], all_); self.assertAlmostEqual(res[label]["total_approved"] - res[label]["total_all"], -veto)
        self.assertIn("heildarhagnadur (50 USDT/kaup)", buf.getvalue())
    def test_planted_advice_increases_the_total_on_every_fee_model(self):
        trades, candles, dec = build_report_env(); env = Env(trades, candles); fill_state(env, dec)
        with redirect_stdout(io.StringIO()): res = sa.report(env.cfg, out=print, now=NOW, B=100)
        env.close()
        for label, c in res.items(): self.assertGreater(c["total_approved"], c["total_all"], label)


class StatsTests(unittest.TestCase):
    def test_net_return_formula(self):
        self.assertAlmostEqual(sa.net_return(100, 100, 0.005), 0.995 / 1.005 - 1); self.assertAlmostEqual(sa.net_return(100, 110, 0.0), 0.10)
        self.assertAlmostEqual(sa.net_return(100, 100, 0.0, 0.0015), 0.9985 / 1.0015 - 1)
        self.assertLess(sa.net_return(100, 100, 0.005), sa.net_return(100, 100, 0.001))
    def test_planted_effect_is_detected(self):
        c = sa.compare(planted(300, 0.04), B=1000); self.assertGreater(c["ci90"][0], 0); self.assertLess(c["p_perm"], 0.01)
        self.assertIn("BAETIR (marktaekt", sa.verdict(c))
    def test_random_labels_are_not_called_significant(self):
        rng = random.Random(3); hits = 0
        for s in range(20):
            rng = random.Random(100 + s); rows = [(rng.gauss(0, 0.01), rng.random() < 0.5) for _ in range(300)]
            if "marktaekt" in sa.verdict(sa.compare(rows, B=400, seed=s)): hits += 1
        self.assertLessEqual(hits, 3)                                                   # ~5% falskt jakvaett vaentanlegt, aldrei stor hluti
    def test_a_worse_advice_is_never_reported_as_helpful(self):
        rows = [(r, not ok) for r, ok in planted(300, 0.04)]; c = sa.compare(rows, B=500)
        self.assertLess(c["diff"], 0); self.assertGreater(c["p_perm"], 0.9); self.assertNotIn("marktaekt", sa.verdict(c))
    def test_diff_equals_manual_difference_of_means(self):
        rows = [(0.02, True), (0.04, True), (-0.01, False), (-0.03, False), (0.0, False)]; c = sa.compare(rows, B=200)
        self.assertAlmostEqual(c["diff"], 0.03 - (-0.04 / 3)); self.assertEqual((c["n_approved"], c["n_vetoed"]), (2, 3))
    def test_too_little_data_is_never_a_verdict(self):
        self.assertIn("OF FA GOGN", sa.verdict(sa.compare(planted(60, 0.2), B=200)))
        self.assertIn("OF FA GOGN", sa.verdict(sa.compare([(0.01, True)] * 250 + [(-0.01, False)] * 10, B=200)))       # hafnad < 30
    def test_empty_group_is_handled(self):
        c = sa.compare([(0.01, True)] * 50, B=100); self.assertIsNone(c["diff"]); self.assertIn("OF FA GOGN", sa.verdict(c))


# ------------------------------------------------------------------ 6. skyrslan fra enda til enda
def build_report_env(n=300, effect=0.03, approve_every=2, live=True, seed=5, approve_rate=None):
    rng = random.Random(seed); trades, candles, decisions = [], [], []
    for i in range(1, n + 1):
        pair = "P%d/USDT" % (i % 40); open_dt = NOW - timedelta(hours=3, minutes=i)
        approved = (i % approve_every == 0) if approve_rate is None else (rng.random() < approve_rate)
        move = (effect if approved else -effect) + rng.gauss(0, 0.004) if effect else rng.gauss(0, 0.01)
        trades.append({"id": i, "pair": pair, "open_rate": 100.0, "open_dt": open_dt, "is_open": 0, "close_rate": 100.0 * (1 + move)})
        decisions.append((i, pair, ft(open_dt), (open_dt + timedelta(minutes=(1 if live else 90))).strftime("%Y-%m-%d %H:%M:%S"), "approve" if approved else "veto"))
    return trades, candles, decisions


def fill_state(env, decisions, version=sa.PROMPT_VERSION):
    for tid, pair, od, da, dec in decisions:
        env.state.execute("INSERT INTO shadow_decisions(trade_id,pair,open_date,decided_at,model,prompt_version,decision,confidence,input_tokens,output_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (tid, pair, od, da, "claude-haiku-5-5", version, dec, 0.7, 400, 40))
    env.state.commit()


class ReportTests(unittest.TestCase):
    def run_report(self, env):
        buf = io.StringIO()
        with redirect_stdout(buf): res = sa.report(env.cfg, out=print, now=NOW, B=600)
        return res, buf.getvalue()
    def test_planted_advice_is_reported_as_helpful_for_every_fee_model(self):
        trades, candles, dec = build_report_env(); env = Env(trades, candles); fill_state(env, dec); res, out = self.run_report(env); env.close()
        self.assertEqual(len(res), 3)
        for label, c in res.items(): self.assertIn("BAETIR (marktaekt", sa.verdict(c), label)
        self.assertEqual(out.count("RADGJOFIN BAETIR (marktaekt"), 3); self.assertIn("Coinbase taker 0.5%", out)
    def test_no_effect_is_not_reported_as_helpful(self):
        trades, candles, dec = build_report_env(effect=0.0); env = Env(trades, candles); fill_state(env, dec); res, out = self.run_report(env); env.close()
        self.assertNotIn("marktaekt", out); self.assertIn("BAETIR EKKI", out)
    def test_decisions_made_long_after_the_trade_opened_are_excluded(self):
        trades, candles, dec = build_report_env(live=False); env = Env(trades, candles); fill_state(env, dec); res, out = self.run_report(env); env.close()
        self.assertIn("300 ekki-lifandi", out); self.assertEqual(res, {})
    def test_open_trades_use_the_latest_fresh_candle_and_stale_ones_are_skipped(self):
        fresh = {"id": 1, "pair": "A/USDT", "open_rate": 100.0, "open_dt": NOW - timedelta(minutes=40), "is_open": 1}
        stale = {"id": 2, "pair": "B/USDT", "open_rate": 100.0, "open_dt": NOW - timedelta(minutes=40), "is_open": 1}
        env = Env([fresh, stale], [("A/USDT", NOW - timedelta(minutes=5), payload(close=110.0)), ("B/USDT", NOW - timedelta(hours=2), payload(close=50.0))])
        fill_state(env, [(1, "A/USDT", ft(fresh["open_dt"]), (fresh["open_dt"] + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), "approve"),
                         (2, "B/USDT", ft(stale["open_dt"]), (stale["open_dt"] + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), "veto")])
        rows, st = sa.load_outcomes(env.cfg, env.state, NOW); env.close()
        self.assertEqual((st["open"], st["stale_open"]), (1, 1)); self.assertEqual(rows[0]["exit"], 110.0)
    def test_report_warns_when_the_advice_filters_nothing(self):
        trades, candles, dec = build_report_env(approve_rate=0.99, effect=0.0); env = Env(trades, candles); fill_state(env, dec); res, out = self.run_report(env); env.close()
        self.assertIn("nanast ALLT", out)
    def test_decisions_from_another_prompt_version_are_never_mixed_in(self):
        trades, candles, dec = build_report_env(n=120); env = Env(trades, candles)
        fill_state(env, dec[:60]); fill_state(env, dec[60:], version="v0-old")
        rows, st = sa.load_outcomes(env.cfg, env.state, NOW, prompt_version=sa.PROMPT_VERSION)
        self.assertEqual(len(rows), 60); self.assertEqual(st["other_version"], 60)
        res, out = self.run_report(env); env.close()
        self.assertIn("60 akvardanir eru fra ANNARRI utgafu", out)
    def test_empty_state_says_so(self):
        env = Env(); buf = io.StringIO()
        with redirect_stdout(buf): self.assertIsNone(sa.report(env.cfg, out=print, now=NOW))
        self.assertIn("Engar akvardanir", buf.getvalue()); env.close()
    def test_cli_report_and_missing_key(self):
        trades, candles, dec = build_report_env(n=60); env = Env(trades, candles); fill_state(env, dec); env.state.close()
        saved = {k: os.environ.get(k) for k in ("TRADES_DB", "AUDIT_DB", "SHADOW_DB", "ANTHROPIC_API_KEY")}
        os.environ.update(TRADES_DB=env.cfg["trades_db"], AUDIT_DB=env.cfg["audit_db"], SHADOW_DB=env.cfg["state_db"]); os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf): rc = sa.main(["--report"])
            self.assertEqual(rc, 0); self.assertIn("Skuggastilling", buf.getvalue()); self.assertIn("OF FA GOGN", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf): rc = sa.main([])
            self.assertEqual(rc, 2); self.assertIn("ANTHROPIC_API_KEY vantar", buf.getvalue())
        finally:
            for k, v in saved.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v
            env.tmp.cleanup()
    def test_end_to_end_once_against_a_local_fake_api(self):
        t = sig_trade(1, 2); env = Env([t], [sig_candle(t)]); api = FakeAPI([OK]); env.cfg["base_url"] = api.url
        made, errors = sa.decide_once(env.cfg, env.state, NOW, sa.make_llm(env.cfg)); api.stop()
        self.assertEqual((made, errors), (1, 0)); self.assertEqual(env.state.execute("SELECT decision, input_tokens FROM shadow_decisions").fetchone(), ("approve", 410))
        self.assertIn("rsi_14", api.requests[0]["body"]["messages"][0]["content"]); env.close()


if __name__ == "__main__":
    unittest.main()
