import copy, json, os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "research"))
import tier_engine as te

D = te.DAY_MS
TBL = {"label": "t", "bnb_discount": 0.0, "tiers": [te._t("A", 0.002, 0.004, 0.0), te._t("B", 0.001, 0.002, 1000.0), te._t("C", 0.0, 0.001, 5000.0)]}


def trade(open_day, close_day, amount=10, open_rate=100.0, close_rate=None, current_rate=None):
    return {"open_ts": int(open_day * D), "close_ts": None if close_day is None else int(close_day * D), "amount": amount,
            "open_rate": open_rate, "close_rate": close_rate, "current_rate": current_rate}


class DataTests(unittest.TestCase):
    def test_coinbase_fees_match_the_users_screenshot(self):
        got = [(t["name"], t["maker"], t["taker"]) for t in te.DEFAULT_TIERS["coinbase"]["tiers"]]
        want = [("Intro", .0025, .0050), ("Advanced 1", .0020, .0040), ("Advanced 2", .0015, .0030), ("Advanced 3", .0010, .0020),
                ("VIP 1", .0006, .0016), ("VIP 2", .0003, .0012), ("VIP 3", .0002, .0010), ("VIP 4", .0001, .0008),
                ("VIP 5", .0, .0006), ("VIP 6", .0, .0004), ("VIP 7", .0, .0003), ("VIP 8", .0, .0002)]
        self.assertEqual(got, want)
    def test_coinbase_thresholds_are_unknown_not_invented(self):
        self.assertEqual(te.missing_thresholds(te.DEFAULT_TIERS["coinbase"]), [t["name"] for t in te.DEFAULT_TIERS["coinbase"]["tiers"][1:]])
    def test_binance_ladder_is_complete_and_monotonic(self):
        b = te.DEFAULT_TIERS["binance"]; self.assertEqual(te.missing_thresholds(b), [])
        self.assertEqual([t["name"] for t in b["tiers"]], ["Regular"] + ["VIP %d" % i for i in range(1, 10)])
        vols = [t["min_volume"] for t in b["tiers"]]; self.assertEqual(vols, sorted(vols))
        self.assertEqual((b["tiers"][0]["maker"], b["tiers"][0]["taker"]), (0.001, 0.001))
    def test_default_tables_validate(self):
        for ex, table in te.DEFAULT_TIERS.items(): self.assertEqual(te.validate_table(table), [], ex)


class TierIndexTests(unittest.TestCase):
    def test_binance_boundaries(self):
        b = te.DEFAULT_TIERS["binance"]
        for vol, name in ((0, "Regular"), (999_999, "Regular"), (1_000_000, "VIP 1"), (19_999_999, "VIP 2"), (20_000_000, "VIP 3"), (5e9, "VIP 9")):
            self.assertEqual(b["tiers"][te.tier_index(b, vol)]["name"], name, vol)
    def test_unknown_thresholds_block_higher_tiers(self):
        c = copy.deepcopy(te.DEFAULT_TIERS["coinbase"]); self.assertEqual(te.tier_index(c, 1e12), 0)          # allt null -> alltaf Intro
        c["tiers"][1]["min_volume"] = 1000.0; c["tiers"][2]["min_volume"] = 5000.0                            # Advanced 3 og áfram enn null
        self.assertEqual(c["tiers"][te.tier_index(c, 10_000)]["name"], "Advanced 2")                          # stoppar við fyrsta óþekkta


class ShippedFileTests(unittest.TestCase):
    PATH = os.path.join(HERE, "..", "tiers.json")
    def test_shipped_tiers_json_loads_and_matches_the_built_in_fees(self):
        tables, warns = te.load_tiers(self.PATH); self.assertEqual(warns, [])
        for ex in ("coinbase", "binance"):
            got = [(x["name"], x["maker"], x["taker"]) for x in tables[ex]["tiers"]]
            want = [(x["name"], x["maker"], x["taker"]) for x in te.DEFAULT_TIERS[ex]["tiers"]]; self.assertEqual(got, want, ex)
        self.assertEqual(tables["binance"]["bnb_discount"], 0.25)
    def test_shipped_file_has_no_invented_coinbase_thresholds(self):
        tables, _ = te.load_tiers(self.PATH); cb = tables["coinbase"]["tiers"]
        self.assertEqual(cb[0]["min_volume"], 0); self.assertTrue(all(x["min_volume"] is None for x in cb[1:]))
        self.assertEqual(len(cb), 12); self.assertEqual(te.missing_thresholds(tables["coinbase"]), [x["name"] for x in cb[1:]])
    def test_shipped_file_is_plain_ascii_json(self):
        raw = open(self.PATH, "rb").read(); self.assertTrue(raw.isascii()); json.loads(raw.decode("ascii"))
    def test_a_fee_edited_in_the_file_is_what_the_simulation_uses(self):
        data = json.load(open(self.PATH)); cb = {x["name"]: x for x in data["coinbase"]["tiers"]}
        cb["Advanced 1"]["min_volume"] = 1000; cb["Advanced 1"]["maker"] = 0.0011; cb["Advanced 1"]["taker"] = 0.0033
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(data, f); f.close()
        tables, _ = te.load_tiers(f.name); os.unlink(f.name)
        r = te.simulate([trade(0, 0.1, close_rate=100.0, amount=5), trade(1, None, current_rate=100.0, amount=1)], tables["coinbase"], now_ms=2 * D, mode="taker")
        self.assertEqual(r["tier"], "Advanced 1"); self.assertAlmostEqual(r["fee_taker"], 0.0033); self.assertAlmostEqual(r["fee_maker"], 0.0011)


class SimulationTests(unittest.TestCase):
    def test_per_leg_fee_follows_the_tier_at_that_moment(self):
        trades = [trade(0, 1, close_rate=110.0), trade(2, 3, close_rate=100.0)]
        r = te.simulate(trades, TBL, now_ms=4 * D, mode="taker")
        # T1: kaup í A (0.4%), sala í B (velta 1000 áður) 0.2%;  T2: bæði í B (0.2%)
        self.assertAlmostEqual(r["closed_net"], (1100 * (1 - .002) - 1000 * 1.004) + (1000 * (1 - .002) - 1000 * 1.002))
        fixed = te.simulate(trades, TBL, now_ms=4 * D, mode="taker", fixed_tier=0)
        self.assertAlmostEqual(fixed["closed_net"], (1100 * .996 - 1000 * 1.004) + (1000 * .996 - 1000 * 1.004))
        self.assertGreater(r["closed_net"], fixed["closed_net"])
    def test_tier_is_decided_by_volume_BEFORE_the_fill(self):
        r = te.simulate([trade(0, None, amount=100, current_rate=100.0)], TBL, now_ms=D, mode="taker")      # 10.000 kaup: rétt yfir C
        self.assertAlmostEqual(r["unrealized"], 10_000 * (1 - .001) - 10_000 * 1.004)                          # kaupið borgar A (0.4%), mat nú í C (0.1%)
        self.assertEqual(r["tier"], "C")
    def test_both_legs_count_toward_volume(self):
        r = te.simulate([trade(0, 0.1, close_rate=100.0, amount=5)], TBL, now_ms=0.2 * D, mode="taker")        # 500 + 500
        self.assertAlmostEqual(r["volume_30d"], 1000.0); self.assertEqual(r["tier"], "B")
    def test_rolling_window_drops_old_volume(self):
        t = [trade(0, 0.1, close_rate=100.0, amount=5)]
        self.assertEqual(te.simulate(t, TBL, now_ms=29 * D, mode="taker")["tier"], "B")
        r = te.simulate(t, TBL, now_ms=31 * D, mode="taker"); self.assertEqual(r["tier"], "A"); self.assertAlmostEqual(r["volume_30d"], 0.0)
    def test_modes_maker_taker_mixed(self):
        t = [trade(0, 0.5, close_rate=102.0)]
        n = lambda m: te.simulate(t, TBL, now_ms=D, mode=m)["closed_net"]
        # kaupið (1.000) borgar A-gjald; salan sér kaup-hliðina í veltunni (1.000) og borgar B-gjald
        self.assertAlmostEqual(n("taker"), 1020 * (1 - .002) - 1000 * 1.004)
        self.assertAlmostEqual(n("maker"), 1020 * (1 - .001) - 1000 * 1.002)
        self.assertAlmostEqual(n("mixed"), 1020 * (1 - .002) - 1000 * 1.002)                                    # kaup maker (A), sala taker (B)
    def test_bnb_discount_and_drag(self):
        b = te.DEFAULT_TIERS["binance"]; t = [trade(0, 0.5, close_rate=100.0)]
        r = te.simulate(t, b, now_ms=D, mode="taker", bnb=True)
        self.assertAlmostEqual(r["closed_net"], 1000 * (1 - .00075) - 1000 * 1.00075)
        r2 = te.simulate(t, b, now_ms=D, mode="taker", drag=0.0015)
        self.assertAlmostEqual(r2["closed_net"], 1000 * (1 - .0015) * (1 - .001) - 1000 * (1 + .0015) * (1 + .001))   # margfaldað, eins og í rannsóknartólinu
        r3 = te.simulate([trade(0, None, current_rate=100.0)], b, now_ms=D, mode="taker", drag=0.0015)
        self.assertAlmostEqual(r3["unrealized"], 1000 * (1 - .0015) * (1 - .001) - 1000 * (1 + .0015) * (1 + .001))    # líka í óloknum viðskiptum
    def test_start_volume_expires_after_the_window(self):
        t = [trade(1, 1.1, close_rate=100.0, amount=1)]
        a = te.simulate(t, TBL, now_ms=2 * D, mode="taker", start_volume=5000.0); self.assertEqual(a["tier"], "C")
        b = te.simulate(t, TBL, now_ms=40 * D, mode="taker", start_volume=5000.0); self.assertEqual(b["tier"], "A")
    def test_open_trade_counts_only_its_buy_leg_and_is_marked_with_current_tier_fee(self):
        r = te.simulate([trade(0, None, amount=20, current_rate=101.0)], TBL, now_ms=D, mode="maker")           # 2.000 velta -> B
        self.assertAlmostEqual(r["volume_30d"], 2000.0)
        self.assertAlmostEqual(r["unrealized"], 2020 * (1 - .001) - 2000 * 1.002)                               # maker: kaup í A (0.2%), sala í B (0.1%)
        self.assertEqual((r["closed_n"], r["open_n"]), (0, 1))
    def test_timeline_records_each_tier_change(self):
        trades = [trade(0, 0.1, close_rate=100.0, amount=5), trade(1, 1.1, close_rate=100.0, amount=45)]
        r = te.simulate(trades, TBL, now_ms=2 * D, mode="taker")
        self.assertEqual([(e["from"], e["tier"]) for e in r["timeline"]], [("A", "B"), ("B", "C")])
    def test_empty_trades_are_safe(self):
        r = te.simulate([], TBL, now_ms=D, mode="taker"); self.assertEqual((r["tier"], r["total"], r["volume_30d"]), ("A", 0.0, 0.0))
    def test_order_of_trades_does_not_matter(self):
        trades = [trade(0, 1, close_rate=110.0), trade(2, 3, close_rate=100.0), trade(4, None, current_rate=99.0)]
        a = te.simulate(trades, TBL, now_ms=5 * D, mode="taker"); b = te.simulate(list(reversed(trades)), TBL, now_ms=5 * D, mode="taker")
        self.assertAlmostEqual(a["total"], b["total"])


class PerTradeTests(unittest.TestCase):
    def test_per_trade_breakdown_matches_totals_and_counts(self):
        trades = [trade(0, 1, close_rate=110.0), trade(2, 3, close_rate=95.0), trade(4, None, current_rate=101.0), trade(5, None)]   # síðasta opið án verðs -> sleppt
        r = te.simulate(trades, TBL, now_ms=6 * D, mode="taker")
        pt = r["trades"]; self.assertEqual(len(pt), 3)
        self.assertAlmostEqual(sum(x["net"] for x in pt if not x["open"]), r["closed_net"])
        self.assertAlmostEqual(sum(x["net"] for x in pt if x["open"]), r["unrealized"])
        self.assertEqual([x["open"] for x in pt], [False, False, True]); self.assertTrue(pt[0]["net"] > 0 > pt[1]["net"])
        self.assertTrue(all(x["cost"] > 0 for x in pt))
    def test_per_trade_cost_includes_the_buy_fee_of_the_tier_at_that_time(self):
        r = te.simulate([trade(0, 1, close_rate=100.0), trade(2, 3, close_rate=100.0)], TBL, now_ms=4 * D, mode="taker")
        self.assertAlmostEqual(r["trades"][0]["cost"], 1000 * 1.004); self.assertAlmostEqual(r["trades"][1]["cost"], 1000 * 1.002)   # seinna kaupið er komið í B-þrep


class PaceTests(unittest.TestCase):
    def test_pace_eta_and_reachability(self):
        # 7 dagar x 1.000 velta/dag = 7.000 kaup síðustu viku -> pace 1.000/dag, steady 30.000
        trades = [trade(4 + i, None, amount=10, current_rate=100.0) for i in range(7)]
        r = te.simulate(trades, TBL, now_ms=10.5 * D, mode="taker")
        self.assertAlmostEqual(r["pace_per_day"], 7000 / 6.5); self.assertAlmostEqual(r["steady_state_30d"], 7000 / 6.5 * 30)      # gögnin ná yfir 6,5 daga
        self.assertEqual(r["tier"], "C"); self.assertIsNone(r["next"])                                         # efsta threp: ekkert naesta
    def test_next_tier_unreachable_at_current_pace(self):
        tbl = copy.deepcopy(TBL); tbl["tiers"][2]["min_volume"] = 1_000_000.0
        trades = [trade(4 + i, None, amount=10, current_rate=100.0) for i in range(7)]
        r = te.simulate(trades, tbl, now_ms=10.5 * D, mode="taker")
        self.assertEqual(r["tier"], "B"); self.assertEqual(r["next"]["name"], "C"); self.assertFalse(r["next"]["reachable"]); self.assertIsNone(r["next"]["eta_days"])
        self.assertAlmostEqual(r["next"]["remaining"], 1_000_000.0 - 7000.0)
    def test_eta_when_reachable(self):
        tbl = copy.deepcopy(TBL); tbl["tiers"][2]["min_volume"] = 20_000.0
        trades = [trade(4 + i, None, amount=10, current_rate=100.0) for i in range(7)]
        r = te.simulate(trades, tbl, now_ms=10.5 * D, mode="taker")
        self.assertTrue(r["next"]["reachable"]); self.assertAlmostEqual(r["next"]["eta_days"], (20_000 - 7000) / (7000 / 6.5))
    def test_pace_uses_the_actual_data_span_when_shorter_than_a_week(self):
        trades = [trade(d, None, amount=10, current_rate=100.0) for d in (0.0, 1.0, 2.0)]                      # 3 x 1.000 yfir 3 daga
        r = te.simulate(trades, TBL, now_ms=3 * D, mode="taker")
        self.assertAlmostEqual(r["pace_per_day"], 1000.0)                                                      # EKKI 3.000/7 = 429
    def test_pace_never_extrapolates_from_less_than_a_day(self):
        r = te.simulate([trade(0.0, None, amount=10, current_rate=100.0)], TBL, now_ms=0.5 * D, mode="taker")
        self.assertAlmostEqual(r["pace_per_day"], 1000.0)                                                      # lágmark 1 dagur: ekki 2.000/dag
    def test_pace_ignores_volume_older_than_a_week(self):
        old = trade(0.0, None, amount=100, current_rate=100.0); new = trade(19.0, None, amount=10, current_rate=100.0)   # 10.000 fyrir 20 d. og 1.000 fyrir 1 d.
        r = te.simulate([old, new], TBL, now_ms=20 * D, mode="taker")
        self.assertAlmostEqual(r["pace_per_day"], 1000.0 / 7.0)                                                # vikuglugginn: aðeins nýja kaupið, deilt í 7
    def test_unknown_next_threshold(self):
        c = te.DEFAULT_TIERS["coinbase"]; r = te.simulate([trade(0, None, current_rate=100.0)], c, now_ms=D, mode="taker")
        self.assertEqual(r["next"]["name"], "Advanced 1"); self.assertIsNone(r["next"]["min_volume"]); self.assertIsNone(r["next"]["eta_days"])


class AnalyzeAndLoadTests(unittest.TestCase):
    def test_analyze_gives_savings_and_missing(self):
        trades = [trade(0, 1, close_rate=110.0), trade(2, 3, close_rate=100.0)]
        a = te.analyze(trades, TBL, 4 * D); m = a["modes"]["taker"]
        self.assertAlmostEqual(m["savings"], m["tiered"]["total"] - m["fixed"]["total"]); self.assertGreater(m["savings"], 0)
        self.assertEqual(a["missing"], [])
    def test_load_tiers_without_file_returns_defaults_and_does_not_mutate_them(self):
        t, w = te.load_tiers(None); t["binance"]["tiers"][1]["min_volume"] = 42.0
        self.assertEqual(te.DEFAULT_TIERS["binance"]["tiers"][1]["min_volume"], 1_000_000.0); self.assertEqual(w, [])
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8"); json.dump(data, f); f.close(); return f.name
    def test_user_can_fill_coinbase_thresholds(self):
        p = self.write({"coinbase": {"tiers": [{"name": "Advanced 1", "min_volume": 10000}, {"name": "Advanced 2", "min_volume": 50000}]}})
        t, w = te.load_tiers(p); os.unlink(p)
        self.assertEqual(t["coinbase"]["tiers"][1]["min_volume"], 10000); self.assertEqual(t["coinbase"]["tiers"][2]["min_volume"], 50000)
        self.assertEqual(len(te.missing_thresholds(t["coinbase"])), 9); self.assertEqual(t["coinbase"]["tiers"][1]["maker"], 0.002)   # gjöld óbreytt
    def test_non_monotonic_thresholds_are_rejected(self):
        p = self.write({"coinbase": {"tiers": [{"name": "Advanced 1", "min_volume": 50000}, {"name": "Advanced 2", "min_volume": 10000}]}})
        with self.assertRaises(ValueError) as cm: te.load_tiers(p)
        os.unlink(p); self.assertIn("LAGARA", str(cm.exception))
    def test_underscore_keys_are_comments_not_warnings(self):
        p = self.write({"_leidbeiningar": "texti", "coinbase": {"tiers": [{"name": "Advanced 1", "min_volume": 1000}]}})
        t, w = te.load_tiers(p); os.unlink(p); self.assertEqual(w, []); self.assertEqual(t["coinbase"]["tiers"][1]["min_volume"], 1000)
    def test_invalid_fee_and_invalid_json_and_unknown_names(self):
        p = self.write({"binance": {"tiers": [{"name": "VIP 1", "maker": 0.9}]}})
        with self.assertRaises(ValueError): te.load_tiers(p)
        os.unlink(p)
        q = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); q.write("{oops"); q.close()
        with self.assertRaises(ValueError) as cm: te.load_tiers(q.name)
        os.unlink(q.name); self.assertIn("ekki gilt JSON", str(cm.exception))
        r = self.write({"kraken": {"tiers": []}, "coinbase": {"tiers": [{"name": "Nope", "min_volume": 1}]}})
        t, w = te.load_tiers(r); os.unlink(r); self.assertEqual(len(w), 2)


if __name__ == "__main__":
    unittest.main()
