import io, os, sqlite3, sys, tempfile, unittest, json
from contextlib import redirect_stdout
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
for sub in (os.path.join("..", "research"), os.path.join("..", "user_data", "strategies")):
    sys.path.insert(0, os.path.join(HERE, sub))
import fee_research as fr
import paper_research as pr

T0 = pd.Timestamp("2026-10-01", tz="UTC")


def frame(close, volume=None, start=T0, gap_after=None):
    n = len(close)
    close = np.asarray(close, float)
    open_ = np.r_[close[0], close[:-1]]
    d = pd.DataFrame({"date": [start + i * fr.STEP + (pd.Timedelta(hours=2) if gap_after is not None and i > gap_after else pd.Timedelta(0)) for i in range(n)],
                      "open": open_, "high": np.maximum(open_, close) * 1.0005, "low": np.minimum(open_, close) * 0.9995, "close": close,
                      "volume": np.ones(n) * 1000 if volume is None else volume})
    return d


def to_db(path, frames):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS candles(pair TEXT, date TEXT, payload TEXT)")
    cols = ["open", "high", "low", "close", "volume"] + fr.FEATURES
    for pair, raw in frames.items():
        f = pr.features(raw).dropna(subset=fr.FEATURES)
        rows = [(pair, str(dt), json.dumps(dict(zip(cols, map(float, vals))))) for dt, vals in zip(f["date"], f[cols].to_numpy())]
        con.executemany("INSERT INTO candles VALUES(?,?,?)", rows)
    con.commit(); con.close()


class FormulaTests(unittest.TestCase):
    def test_net_return_formula(self):
        self.assertAlmostEqual(fr.net_return(100, 100, 0.0, 0.0), 0.0)
        self.assertAlmostEqual(fr.net_return(100, 110, 0.0, 0.0), 0.10)
        want = (110 * (1 - 0.0015) * (1 - 0.005)) / (100 * (1 + 0.0015) * (1 + 0.005)) - 1
        self.assertAlmostEqual(fr.net_return(100, 110, 0.005), want)
    def test_flat_trade_costs_about_1_3_percent_at_coinbase(self):
        self.assertAlmostEqual(fr.net_return(100, 100, 0.005), -0.0130, delta=0.0003)       # 2*(0.5% + 0.15%) = 1.3%
        self.assertAlmostEqual(fr.net_return(100, 100, 0.001), -0.0050, delta=0.0003)       # 2*(0.1% + 0.15%) = 0.5%


def mk(rsi, close, high=None, low=None, open_=None):
    n = len(rsi); close = np.asarray(close, float)
    open_ = np.r_[close[0], close[:-1]] if open_ is None else np.asarray(open_, float)
    d = pd.DataFrame({"date": [T0 + i * fr.STEP for i in range(n)], "open": open_, "close": close,
                      "high": np.maximum(open_, close) if high is None else np.asarray(high, float),
                      "low": np.minimum(open_, close) if low is None else np.asarray(low, float), "rsi_14": np.asarray(rsi, float)})
    for f in fr.FEATURES:
        if f not in d: d[f] = 0.0
    return d


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.rsi = [50] * 45 + [25, 40, 55, 75, 60, 60, 60, 60]          # merki (RSI<30) á kerti 45; RSI>70 á kerti 48
        self.close = [100.0] * 45 + [99.0, 100.0, 101.0, 102.0, 101.0, 101.0, 101.0, 101.0]
    def test_entry_at_next_open_and_exit_at_open_after_signal(self):
        open_ = np.r_[self.close[0], self.close[:-1]]; open_[46] = 99.6; open_[49] = 101.7     # opnun != lokaverð fyrra kerti (bil milli kerta)
        s = mk(self.rsi, self.close, open_=open_)
        ev = fr.run_segment(s, fr.Policy("rsi70", rsi_exit=70)); e = ev[0]
        self.assertEqual(pd.Timestamp(e["signal_time"]), s["date"].iloc[45])
        self.assertEqual(pd.Timestamp(e["entry_time"]), s["date"].iloc[46]); self.assertAlmostEqual(e["entry"], 99.6)               # kaup á OPNUN næsta kertis (ekki 99,0 = lokaverð merkis)
        self.assertEqual(pd.Timestamp(e["exit_time"]), s["date"].iloc[49]); self.assertAlmostEqual(e["exit"], 101.7)     # RSI>70 á kerti 48 -> sala á OPNUN 49 (ekki lokaverð 102,0 á kerti 48)
        self.assertEqual((e["reason"], e["resolved"]), ("signal", True))
    def test_stop_loss_fires_at_minus_5_percent_and_checked_before_signal(self):
        low = list(self.close); low[47] = 90.0
        s = mk(self.rsi, self.close, low=low)
        e = fr.run_segment(s, fr.Policy("rsi70", rsi_exit=70), stop=0.05)[0]
        self.assertEqual(e["reason"], "stop"); self.assertAlmostEqual(e["exit"] / e["entry"], 0.95)
    def test_take_profit_exit_is_the_limit_price(self):
        high = list(self.close); high[48] = 103.5
        s = mk(self.rsi, self.close, high=high)
        e = fr.run_segment(s, fr.Policy("tp2", tp=0.02))[0]
        self.assertEqual(e["reason"], "tp"); self.assertAlmostEqual(e["exit"] / e["entry"], 1.02)
    def test_min_gain_skips_a_signal_that_would_not_cover_fees(self):
        rsi = [50] * 45 + [25, 40, 72, 60, 75, 60, 60]                   # RSI>70 á kerti 47 (hagnaður ~0.5%) og aftur á 49 (hagnaður ~2.5%)
        close = [100.0] * 45 + [99.0, 99.2, 99.8, 99.5, 102.0, 101, 101]          # kaupverð 99,0: +0,8% á kerti 47, +3,0% á kerti 49
        s = mk(rsi, close)
        plain = fr.run_segment(s, fr.Policy("a", rsi_exit=70))[0]
        gated = fr.run_segment(s, fr.Policy("b", rsi_exit=70, min_gain=0.013))[0]
        self.assertEqual(pd.Timestamp(plain["exit_time"]), s["date"].iloc[48])      # selur strax á fyrsta RSI>70
        self.assertEqual(pd.Timestamp(gated["exit_time"]), s["date"].iloc[50])      # bíður þar til hagnaður dekkar gjöld
    def test_unresolved_trade_is_kept_and_marked_to_market(self):
        rsi = [50] * 45 + [25, 40, 45, 45]
        s = mk(rsi, [100.0] * 45 + [99.0, 100.0, 98.0, 97.5])
        ev = fr.run_segment(s, fr.Policy("rsi70", rsi_exit=70))
        self.assertEqual(len(ev), 1); self.assertFalse(ev[0]["resolved"]); self.assertEqual(ev[0]["reason"], "open")
        self.assertAlmostEqual(ev[0]["exit"], 97.5)                                   # metið á lokaverði, EKKI hent
    def test_timeout_exit_and_reentry_after_exit(self):
        old = fr.MAX_HOLD; fr.MAX_HOLD = 4
        try:
            rsi = [50] * 45 + [25] + [40] * 8 + [25] + [40] * 8
            close = [100.0] * 45 + [99.0] + [100.0] * 8 + [99.0] + [100.0] * 8
            ev = fr.run_segment(mk(rsi, close), fr.Policy("rsi70", rsi_exit=70))
        finally:
            fr.MAX_HOLD = old
        self.assertEqual([e["reason"] for e in ev[:2]], ["timeout", "timeout"])       # tvö aðskilin kaup, seinna merkið eftir útgöngu
    def test_no_new_entry_while_in_position(self):
        rsi = [50] * 45 + [25, 25, 25, 40, 75, 40, 40, 40]                            # merki á 45; RSI<30 aftur á 46,47 MEÐAN staðan er opin; sala eftir RSI>70 á 49
        close = [100.0] * 45 + [99.0, 99.0, 99.0, 100.0, 103.0, 102.0, 102.0, 102.0]
        ev = fr.run_segment(mk(rsi, close), fr.Policy("rsi70", rsi_exit=70))
        self.assertEqual(len(ev), 1)                                                   # eins og botninn: ein staða á hvert par
    def test_gap_splits_segments(self):
        a = mk([50] * 60, [100.0] * 60); b = mk([50] * 60, [100.0] * 60); b["date"] = b["date"] + pd.Timedelta(hours=10)
        d = pd.concat([a, b]).assign(pair="X/USDT")
        segs = list(fr.segments(d)); self.assertEqual(len(segs), 2)
        for _, s in segs: self.assertTrue((s["date"].diff().dropna() == fr.STEP).all())


def synthetic_db(path, edge, pairs=12, n=6000, seed=3):
    """Tilbúinn markaður. edge=True: á 40% þeirra kerta þar sem RSI fer FYRST undir 30 kemur magnstoppur og verð hækkar ~3,7% á
    næstu klst. (planted forskot sem líkanið á að finna út frá magni); hin 60% fá ekkert. edge=False: hrein random walk."""
    rng = np.random.default_rng(seed); frames = {}
    for pi in range(pairs):
        ret = rng.normal(0, 0.003, n)
        vol = rng.lognormal(0, 0.3, n) * 1000
        close = 100 * np.cumprod(1 + ret)
        if edge:
            rsi = pr.features(frame(close, volume=vol))["rsi_14"].to_numpy()
            first = np.where((rsi[1:] < 30) & (rsi[:-1] >= 30))[0] + 1
            for i in first:
                if i + 13 < n and edge == "uniform":                   # ALLIR RSI<30 fá sama forskot: reglan ein og sér er jákvæð, líkanið bætir engu við
                    ret[i + 1:i + 13] += 0.0030
                elif i + 13 < n and rng.random() < 0.4:
                    vol[i] *= 4
                    ret[i + 1:i + 13] += 0.0030
            close = 100 * np.cumprod(1 + ret)
        frames["P%d/USDT" % pi] = frame(close, volume=vol)
        if pi == 0: frames["BTC/USDT"] = frame(close * 800, volume=vol)
    to_db(path, frames)


class ResearchTests(unittest.TestCase):
    def run_report(self, edge):
        tmp = tempfile.TemporaryDirectory(); p = os.path.join(tmp.name, "a.sqlite"); synthetic_db(p, edge)
        d = fr.load_candles(p); buf = io.StringIO()
        with redirect_stdout(buf): res = fr.report(d, out=lambda s: print(s))
        tmp.cleanup(); return res, buf.getvalue()
    def test_planted_edge_is_found_out_of_sample(self):
        res, out = self.run_report(True)
        sets = {s["name"]: s for s in res["wf"]["sets"]}
        self.assertGreater(sets["efsti 20% spar"]["mean"], sets["allt (grunnlina)"]["mean"] + 0.005)      # likanið velur betri kaup en meðaltalið
        self.assertGreater(sets["efsti 20% spar"]["diff_lo"], 0.0)                                         # og MUNURINN á vali og meðaltali er marktækur
        self.assertTrue(res["verdict"].startswith("LIKAN BAETIR:"), res["verdict"])
        self.assertTrue(res["base_verdict"].startswith("RSI-reglan ein og ser: JAKVAED"), res["base_verdict"])
    def test_positive_base_rule_does_not_make_the_model_look_good(self):
        res, out = self.run_report("uniform")
        self.assertTrue(res["base_verdict"].startswith("RSI-reglan ein og ser: JAKVAED"), res["base_verdict"])   # reglan sjálf er jákvæð eftir gjöld...
        self.assertTrue(res["verdict"].startswith("LIKAN BAETIR EKKI"), res["verdict"])                           # ...en líkanið er ekki betra en meðaltalið
    def test_random_walk_has_no_edge_and_model_does_not_invent_one(self):
        res, out = self.run_report(False)
        sets = {s["name"]: s for s in res["wf"]["sets"]}
        self.assertLess(sets["allt (grunnlina)"]["mean"], -0.005)                                          # kostnaður ~1,3% -> tap
        self.assertTrue(res["verdict"].startswith("LIKAN BAETIR EKKI"), res["verdict"])
        self.assertTrue(res["base_verdict"].startswith("RSI-reglan ein og ser: TAPAR"), res["base_verdict"])
        self.assertIn("RSI>70 og hagnadur>=1.3%", out); self.assertIn("take-profit +2%", out)
    def test_random_walk_false_positive_rate_is_low(self):
        wins = 0; runs = 6
        for seed in range(runs):
            tmp = tempfile.TemporaryDirectory(); p = os.path.join(tmp.name, "a.sqlite"); synthetic_db(p, False, seed=100 + seed, pairs=10, n=5000)
            d = fr.load_candles(p)
            with redirect_stdout(io.StringIO()): res = fr.report(d, out=lambda s: None)
            tmp.cleanup()
            self.assertNotIn("error", res["wf"], res["wf"])                                                # prófið er EKKI tómt: líkanið var raunverulega metið
            wins += 1 if (res["verdict"] or "").startswith("LIKAN BAETIR:") else 0
        self.assertLessEqual(wins, 1)                                                                      # að hámarki 1 af 6 tilbúnum "fundum" í hreinum hávaða
    def test_too_little_data_is_reported_not_crashed(self):
        tmp = tempfile.TemporaryDirectory(); p = os.path.join(tmp.name, "a.sqlite"); synthetic_db(p, False, pairs=1, n=600)
        d = fr.load_candles(p); buf = io.StringIO()
        with redirect_stdout(buf): fr.report(d, out=lambda s: print(s))
        self.assertIn("dagar af gognum", buf.getvalue()); tmp.cleanup()
    def test_days_filter(self):
        tmp = tempfile.TemporaryDirectory(); p = os.path.join(tmp.name, "a.sqlite"); synthetic_db(p, False, pairs=2, n=2000)
        full = fr.load_candles(p); part = fr.load_candles(p, days=1)
        self.assertLess(len(part), len(full)); tmp.cleanup()


class TierSectionTests(unittest.TestCase):
    def make(self, tiers=None, **kw):
        tmp = tempfile.TemporaryDirectory(); p = os.path.join(tmp.name, "a.sqlite"); synthetic_db(p, False, pairs=6, n=4000)
        tp = None
        if tiers is not None:
            tp = os.path.join(tmp.name, "tiers.json")
            with open(tp, "w") as f: f.write(tiers if isinstance(tiers, str) else json.dumps(tiers))
        d = fr.load_candles(p); buf = io.StringIO()
        with redirect_stdout(buf): res = fr.report(d, out=lambda s: print(s), tiers_path=tp, **kw)
        return tmp, res, buf.getvalue(), p
    def test_thresholds_make_the_bot_climb_and_save_fees(self):
        tmp, res, out, _ = self.make({"coinbase": {"tiers": [{"name": "Advanced 1", "min_volume": 1000}, {"name": "Advanced 2", "min_volume": 4000}]}})
        cb = res["tiers"]["coinbase"]["modes"]; bn = res["tiers"]["binance"]["modes"]
        self.assertGreater(cb["taker"]["savings"], 0); self.assertGreater(cb["maker"]["savings"], 0)
        self.assertEqual(cb["taker"]["tiered"]["tier"], "Advanced 2"); self.assertEqual(bn["taker"]["tiered"]["tier"], "Regular")
        self.assertAlmostEqual(bn["taker"]["savings"], 0.0)
        for needle in ("5. Gjaldathrep", "Coinbase Advanced", "threpabreyting", "sparnadur", "naesta threp"): self.assertIn(needle, out)
        tmp.cleanup()
    def test_without_thresholds_coinbase_stays_in_intro_and_says_what_is_missing(self):
        tmp, res, out, _ = self.make()
        self.assertEqual(res["tiers"]["coinbase"]["modes"]["taker"]["tiered"]["tier"], "Intro")
        self.assertIn("throskuldar vantar fyrir 11 threp", out); self.assertAlmostEqual(res["tiers"]["coinbase"]["modes"]["taker"]["savings"], 0.0); tmp.cleanup()
    def test_invalid_tiers_file_is_reported(self):
        tmp, res, out, _ = self.make('{"coinbase": {"tiers": [{"name": "Advanced 1", "min_volume": 9000}, {"name": "Advanced 2", "min_volume": 10}]}}')
        self.assertIn("VILLA", out); self.assertIn("LAGARA", out); self.assertIsNone(res["tiers"]); tmp.cleanup()
    def test_fixed_intro_total_matches_the_per_trade_research_numbers(self):
        tmp, res, out, p = self.make(stake=30.0)
        ev = fr.build_events(fr.load_candles(p), fr.POLICIES[0])
        # rannsóknartólið gefur nettóávöxtun MIÐAÐ VIÐ KOSTNAÐ (með gjöldum): USDT = 30 * (1+skrið) * (1+gjald) * ávöxtun
        want = float((30.0 * (1 + fr.DRAG) * (1 + 0.005) * ev["net Coinbase 0.5%"]).sum())
        got = res["tiers"]["coinbase"]["modes"]["taker"]["fixed"]["total"]
        self.assertAlmostEqual(got, want, delta=0.02); tmp.cleanup()                       # sama líkan -> nánast nákvæmlega sama tala
    def test_cli_finds_tiers_json_next_to_the_data_folder(self):
        root = tempfile.TemporaryDirectory(); ud = os.path.join(root.name, "user_data"); os.makedirs(ud)
        synthetic_db(os.path.join(ud, "paper_audit.sqlite"), False, pairs=4, n=3000)
        with open(os.path.join(root.name, "tiers.json"), "w") as f: json.dump({"coinbase": {"tiers": [{"name": "Advanced 1", "min_volume": 500}]}}, f)
        buf = io.StringIO()
        with redirect_stdout(buf): fr.main(["--db", os.path.join(ud, "paper_audit.sqlite"), "--stake", "50"])
        out = buf.getvalue(); self.assertIn("50 USDT per kaup", out); self.assertIn("Advanced 1", out); root.cleanup()


class MakerTests(unittest.TestCase):
    def test_maker_scenario_sits_between_binance_and_coinbase_taker(self):
        tmp = tempfile.TemporaryDirectory(); p = os.path.join(tmp.name, "a.sqlite"); synthetic_db(p, False, pairs=4, n=3000)
        d = fr.load_candles(p); ev = fr.build_events(d, fr.POLICIES[0])
        b, m, c = (ev["net " + k].mean() for k in ("Binance 0.1%", "Coinbase maker 0.25%", "Coinbase 0.5%"))
        self.assertGreater(b, m); self.assertGreater(m, c)
        self.assertAlmostEqual(fr.net_return(100, 100, 0.0025), -0.0080, delta=0.0003)       # 2*(0.25% + 0.15%) = 0.8%
        buf = io.StringIO()
        with redirect_stdout(buf): fr.report(d, out=lambda s: print(s))
        out = buf.getvalue(); self.assertIn("Coinbase maker 0.25%", out); self.assertIn("CB maker", out); tmp.cleanup()


class CensoringTests(unittest.TestCase):
    def test_trades_cut_by_a_gap_or_a_pair_leaving_are_excluded_but_open_at_data_end_are_kept(self):
        rsi = [50] * 45 + [25, 40, 40, 40, 40, 40, 40, 40]
        close = [100.0] * 45 + [99.0, 99.5, 99.0, 99.2, 99.0, 99.1, 99.0, 99.2]
        a = mk(rsi, close).assign(pair="GONE/USDT")                                       # parið hættir að berast 30+ mín. fyrir lok gagnanna
        long_ = mk(rsi + [40] * 30, close + [99.0] * 30).assign(pair="ALIVE/USDT")       # parið er til loka gagnanna, staðan enn opin
        d = pd.concat([a, long_]).reset_index(drop=True)
        ev = fr.build_events(d, fr.Policy("rsi70", rsi_exit=70))
        self.assertEqual(ev.attrs["censored"], 1)                                          # GONE útilokað
        self.assertEqual(list(ev["pair"]), ["ALIVE/USDT"])
        self.assertFalse(ev["resolved"].iloc[0]); self.assertEqual(ev["reason"].iloc[0], "open")   # metið á lokaverði
    def test_report_prints_censored_count_and_daily_table(self):
        tmp = tempfile.TemporaryDirectory(); p = os.path.join(tmp.name, "a.sqlite"); synthetic_db(p, False, pairs=4, n=3000)
        buf = io.StringIO()
        with redirect_stdout(buf): fr.report(fr.load_candles(p), out=lambda s: print(s))
        out = buf.getvalue(); self.assertIn("Utilokad (RSI>70):", out); self.assertIn("Dag fyrir dag", out); tmp.cleanup()


class WalkForwardTests(unittest.TestCase):
    def test_purge_removes_training_events_that_overlap_the_test_period(self):
        n = 400; rng = np.random.default_rng(0); t0 = pd.Timestamp("2026-10-01", tz="UTC")
        ev = pd.DataFrame({"entry_time": [t0 + pd.Timedelta(hours=i) for i in range(n)], "exit_time": [t0 + pd.Timedelta(hours=i + 2) for i in range(n)]})   # hvert kaup stendur í 2 klst.
        for f in fr.MODEL_FEATURES: ev[f] = rng.normal(size=n)
        ev["net Coinbase 0.5%"] = rng.normal(0, 0.02, n)
        res = fr.walk_forward(ev, "net Coinbase 0.5%", train_frac=0.6); cut = int(n * 0.6)
        self.assertEqual(res["train_n"], cut - 2)          # kaup nr. cut-2 og cut-1 enda eftir að prófunartíminn byrjar -> EKKI í þjálfun
        self.assertEqual(res["test_n"], n - cut)
    def test_model_is_scored_only_on_the_later_period(self):
        n = 400; rng = np.random.default_rng(1); t0 = pd.Timestamp("2026-10-01", tz="UTC")
        ev = pd.DataFrame({"entry_time": [t0 + pd.Timedelta(hours=i) for i in range(n)], "exit_time": [t0 + pd.Timedelta(hours=i + 1) for i in range(n)]})
        for f in fr.MODEL_FEATURES: ev[f] = rng.normal(size=n)
        ev["net Coinbase 0.5%"] = np.where(np.arange(n) < 240, 0.05, -0.05)               # fyrri hluti alltaf +5%, seinni alltaf -5% (ekkert lærist á seinni hlutann)
        res = fr.walk_forward(ev, "net Coinbase 0.5%", train_frac=0.6)
        base = [s for s in res["sets"] if s["name"].startswith("allt")][0]
        self.assertAlmostEqual(base["mean"], -0.05)         # grunnlínan er mæld á SEINNI hlutanum
    def test_too_few_events_returns_error_not_crash(self):
        ev = pd.DataFrame({"entry_time": pd.date_range("2026-10-01", periods=50, freq="h", tz="UTC"), "exit_time": pd.date_range("2026-10-01 01:00", periods=50, freq="h", tz="UTC")})
        for f in fr.MODEL_FEATURES: ev[f] = 0.0
        ev["net Coinbase 0.5%"] = 0.0
        self.assertIn("error", fr.walk_forward(ev, "net Coinbase 0.5%"))


class DiffCITests(unittest.TestCase):
    def test_diff_ci_detects_a_real_difference_and_not_a_fake_one(self):
        t = pd.date_range("2026-10-01", periods=600, freq="30min", tz="UTC"); rng = np.random.default_rng(2)
        v = rng.normal(0.0, 0.02, 600); mask = np.zeros(600, bool); mask[::5] = True
        lo, hi = fr.block_diff_ci(t, v, mask); self.assertLess(lo, 0.0); self.assertGreater(hi, 0.0)       # ekkert raunverulegt forskot: bilið nær yfir 0
        v2 = v.copy(); v2[mask] += 0.03
        lo2, hi2 = fr.block_diff_ci(t, v2, mask); self.assertGreater(lo2, 0.0)                              # raunverulegt +3%: bilið er yfir 0
    def test_diff_ci_needs_enough_blocks(self):
        t = pd.date_range("2026-10-01", periods=40, freq="30min", tz="UTC")
        lo, hi = fr.block_diff_ci(t, np.zeros(40), np.arange(40) % 2 == 0); self.assertTrue(np.isnan(lo))


class CITests(unittest.TestCase):
    def test_block_ci_contains_mean_and_is_ordered(self):
        t = pd.date_range("2026-10-01", periods=400, freq="30min", tz="UTC"); v = np.random.default_rng(1).normal(0.002, 0.02, 400)
        lo, hi = fr.block_ci(t, v); self.assertLess(lo, v.mean()); self.assertGreater(hi, v.mean())


if __name__ == "__main__":
    unittest.main()
