#!/usr/bin/env python3
"""
Freqtrade-mælaborð í Coinbase Advanced Trade stíl: kertagraf + magn,
pöntunarbók (order book), og LIFANDI viðskiptasaga/staða beint úr
Freqtrade's eigin API (ekki CSV-skjali eins og einfaldi botninn).

HVERNIG ÞETTA VIRKAR
---------------------
1. Skráir sig inn á Freqtrade's API (sama notandanafn/lykilorð og FreqUI).
2. Sækir opnar/lokaðar stöður og heildarjöfnuð beint þaðan.
3. Kertagraf og pöntunarbók koma frá Binance Testnet's opinbera API (sömu
   gögn og Freqtrade sjálft er að versla með í dry-run).

Þetta er ALGJÖRLEGA AÐSKILIÐ frá Freqtrade sjálfu - les bara gögn, breytir
engu.

UPPSETNING
----------
pip install flask requests python-dotenv --break-system-packages

Í .env (sama mappa og þetta skjal - C:\\Users\\bjarn\\Documents\\freqtrade\\):
    FREQTRADE_PAPER_API_URL=http://127.0.0.1:8082
    FREQTRADE_PAPER_USERNAME=bjarni
    FREQTRADE_PAPER_PASSWORD=lykilorðið-úr-config.paper.json

Keyrðu: python3 Freqtrade_dashboard_paper.py   (port 5051)
Opnaðu: http://localhost:5051
"""

import os
import sys

import requests
from flask import Flask, jsonify, render_template_string, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

FT_API_URL = os.environ.get("FREQTRADE_PAPER_API_URL", "http://127.0.0.1:8082")
FT_USERNAME = os.environ.get("FREQTRADE_PAPER_USERNAME", "bjarni")
FT_PASSWORD = os.environ.get("FREQTRADE_PAPER_PASSWORD")

# Paper-botninn notar RAUNVERULEGAN markað (exchange.sandbox=false), svo verð, kerti og order book koma þaðan.
MARKET_BASE_URL = os.environ.get("MARKET_DATA_URL", "https://api.binance.com")

AVAILABLE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT",
]

if not FT_USERNAME or not FT_PASSWORD:
    print("Vantar FREQTRADE_PAPER_PASSWORD í .env skjalinu.")
    sys.exit(1)

_auth = {"access_token": None}


def login():
    resp = requests.post(
        f"{FT_API_URL}/api/v1/token/login",
        auth=(FT_USERNAME, FT_PASSWORD),
        timeout=10,
    )
    resp.raise_for_status()
    _auth["access_token"] = resp.json()["access_token"]


def ft_get(path):
    if not _auth["access_token"]:
        login()
    headers = {"Authorization": f"Bearer {_auth['access_token']}"}
    resp = requests.get(f"{FT_API_URL}{path}", headers=headers, timeout=10)
    if resp.status_code == 401:
        login()
        headers = {"Authorization": f"Bearer {_auth['access_token']}"}
        resp = requests.get(f"{FT_API_URL}{path}", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


# --- Freqtrade-gögn: staða, jöfnuður, viðskipti ---

@app.route("/api/performance")
def api_performance():
    """Heildarsamantekt á frammistöðu stefnunnar - Freqtrade reiknar þetta
    sjálft út frá allri viðskiptasögunni í gagnagrunninum."""
    try:
        profit = ft_get("/api/v1/profit")
        return jsonify({
            "profit_closed_coin": profit.get("profit_closed_coin", 0),
            "profit_closed_pct": profit.get("profit_closed_percent", 0),
            "profit_all_coin": profit.get("profit_all_coin", 0),
            "profit_all_pct": profit.get("profit_all_percent", 0),
            "trade_count": profit.get("trade_count", 0),
            "closed_trade_count": profit.get("closed_trade_count", 0),
            "winning_trades": profit.get("winning_trades", 0),
            "losing_trades": profit.get("losing_trades", 0),
            "winrate": profit.get("winrate", 0),
            "best_pair": profit.get("best_pair", "-"),
            "best_pair_profit_pct": profit.get("best_pair_profit_ratio", 0) * 100,
            "avg_duration": profit.get("avg_duration", "-"),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# --- Samanburður: sömu viðskipti með Binance- og Coinbase-gjöldum ---
# Gjöldin eru hlutfall (0.001 = 0,1%, 0.005 = 0,5%) og eru á HVORA hlið (kaup og sala).
# Má breyta í .env: COMPARE_FEE_BINANCE=0.001 og COMPARE_FEE_COINBASE=0.005
# --- tier-engine-start
import copy
import json
import os
from collections import deque

DAY_MS = 86_400_000


def _t(name, maker, taker, vol):
    return {"name": name, "maker": maker, "taker": taker, "min_volume": vol}


DEFAULT_TIERS = {
    "coinbase": {
        "label": "Coinbase Advanced (spot)",
        "bnb_discount": 0.0,
        # Gjold: af skjamynd frá notanda (Fees-flipinn). Throskuldar ($ 30 daga velta) vantar: null -> fylla inn i tiers.json.
        "tiers": [
            _t("Intro", 0.0025, 0.0050, 0.0),
            _t("Advanced 1", 0.0020, 0.0040, None),
            _t("Advanced 2", 0.0015, 0.0030, None),
            _t("Advanced 3", 0.0010, 0.0020, None),
            _t("VIP 1", 0.0006, 0.0016, None),
            _t("VIP 2", 0.0003, 0.0012, None),
            _t("VIP 3", 0.0002, 0.0010, None),
            _t("VIP 4", 0.0001, 0.0008, None),
            _t("VIP 5", 0.0000, 0.0006, None),
            _t("VIP 6", 0.0000, 0.0004, None),
            _t("VIP 7", 0.0000, 0.0003, None),
            _t("VIP 8", 0.0000, 0.0002, None),
        ],
    },
    "binance": {
        "label": "Binance spot (BNB-skilyrdi gert rad fyrir)",
        "bnb_discount": 0.25,
        # Afleiddar heimildir 2026 (ekki opinbera sidan sjalf) - stadfestu a gjaldasidu Binance.
        "tiers": [
            _t("Regular", 0.00100, 0.00100, 0.0),
            _t("VIP 1", 0.00090, 0.00100, 1_000_000.0),
            _t("VIP 2", 0.00080, 0.00100, 5_000_000.0),
            _t("VIP 3", 0.00040, 0.00060, 20_000_000.0),
            _t("VIP 4", 0.00040, 0.00052, 75_000_000.0),
            _t("VIP 5", 0.00025, 0.00031, 150_000_000.0),
            _t("VIP 6", 0.00020, 0.00029, 400_000_000.0),
            _t("VIP 7", 0.00019, 0.00028, 800_000_000.0),
            _t("VIP 8", 0.00016, 0.00025, 2_000_000_000.0),
            _t("VIP 9", 0.00011, 0.00023, 4_000_000_000.0),
        ],
    },
}


def validate_table(table):
    """Skilar lista af villum (tomur = i lagi)."""
    errs, last = [], -1.0
    tiers = table["tiers"]
    if not tiers or tiers[0].get("min_volume") != 0:
        errs.append("fyrsta threp verdur ad hafa min_volume 0")
    for t in tiers:
        for k in ("maker", "taker"):
            v = t.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v <= 0.05):
                errs.append("%s: %s verdur ad vera tala milli 0 og 0.05 (0.0025 = 0,25%%)" % (t.get("name"), k))
        v = t.get("min_volume")
        if v is None:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            errs.append("%s: min_volume verdur ad vera tala >= 0 eda null" % t.get("name"))
            continue
        if v < last:
            errs.append("%s: min_volume (%s) er LAGARA en i threpi a undan - throskuldar eiga ad haekka" % (t.get("name"), v))
        last = max(last, v)
    return errs


def load_tiers(path=None):
    """DEFAULT_TIERS + valfrjáls yfirskrift ur JSON-skra (eftir nafni threps). Skilar (toflur, vidvaranir). Kastar ValueError ef skrain er ogild."""
    tables = copy.deepcopy(DEFAULT_TIERS)
    warnings = []
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except ValueError as exc:
            raise ValueError("tiers.json er ekki gilt JSON: %s" % exc)
        for ex, spec in (data or {}).items():
            if str(ex).startswith("_"):
                continue                               # leidbeiningar/athugasemdir i skranni
            if ex not in tables or not isinstance(spec, dict):
                warnings.append("tiers.json: thekki kaupholl '%s' sleppt" % ex)
                continue
            if "bnb_discount" in spec:
                tables[ex]["bnb_discount"] = spec["bnb_discount"]
            by_name = {t["name"]: t for t in tables[ex]["tiers"]}
            for over in spec.get("tiers", []):
                t = by_name.get(over.get("name"))
                if t is None:
                    warnings.append("tiers.json: threp '%s' ekki til fyrir %s - sleppt" % (over.get("name"), ex))
                    continue
                for k in ("maker", "taker", "min_volume"):
                    if k in over:
                        t[k] = over[k]
    for ex, table in tables.items():
        errs = validate_table(table)
        if errs:
            raise ValueError("%s: %s" % (ex, "; ".join(errs)))
    return tables, warnings


def missing_thresholds(table):
    return [t["name"] for t in table["tiers"][1:] if t["min_volume"] is None]


def tier_index(table, volume):
    idx = 0
    for i, t in enumerate(table["tiers"]):
        if t["min_volume"] is None:
            break                                  # throskuldur othekktur: haerri threp otiltaek
        if volume >= t["min_volume"]:
            idx = i
        else:
            break
    return idx


def _fee(table, tier_idx, side, bnb):
    f = table["tiers"][tier_idx][side]
    return f * (1 - table.get("bnb_discount", 0.0)) if bnb else f


def _side(leg, mode):
    if mode == "maker":
        return "maker"
    if mode == "mixed":
        return "maker" if leg == "buy" else "taker"
    return "taker"


def simulate(trades, table, now_ms, mode="taker", start_volume=0.0, window_days=30, bnb=False, drag=0.0, fixed_tier=None):
    """
    trades: [{open_ts, close_ts|None, amount, open_rate, close_rate|None, current_rate|None}] (ts i millisek.)
    mode: 'taker' (bædar hlidar markadspontun), 'maker' (bædar limit), 'mixed' (kaup maker, sala taker)
    fixed_tier: ef gefinn (vísitala) - gjald ALLTAF ur thvi threpi (til samanburdar)
    """
    window = int(window_days * DAY_MS)
    events = []
    for i, t in enumerate(trades):
        events.append((t["open_ts"], 0, i, "buy", t["amount"] * t["open_rate"]))
        if t.get("close_ts") and t.get("close_rate"):
            events.append((t["close_ts"], 1, i, "sell", t["amount"] * t["close_rate"]))
    events.sort()
    dq, vol = deque(), 0.0
    if start_volume > 0 and events:
        dq.append((events[0][0] - 1, start_volume))
        vol = start_volume
    buy_fee, sell_fee, timeline, fees_paid = {}, {}, [], 0.0
    last_idx = 0 if fixed_tier is None else fixed_tier
    for ts, _, i, leg, notional in events:
        while dq and dq[0][0] <= ts - window:
            vol -= dq.popleft()[1]
        vol = max(vol, 0.0)
        idx = tier_index(table, vol) if fixed_tier is None else fixed_tier
        if idx != last_idx:
            timeline.append({"ts": ts, "tier": table["tiers"][idx]["name"], "from": table["tiers"][last_idx]["name"], "volume": vol})
            last_idx = idx
        fee = _fee(table, idx, _side(leg, mode), bnb)
        (buy_fee if leg == "buy" else sell_fee)[i] = fee
        fees_paid += notional * fee
        dq.append((ts, notional))
        vol += notional
    while dq and dq[0][0] <= now_ms - window:
        vol -= dq.popleft()[1]
    vol = max(vol, 0.0)
    now_idx = tier_index(table, vol) if fixed_tier is None else fixed_tier
    closed_net = unreal = 0.0
    closed_n = open_n = 0
    per_trade = []        # eitt stak fyrir hvert viðskipti (í sömu röð og inntak): net USDT, kostnaðarverð, hvort opið
    for i, t in enumerate(trades):
        cost = t["amount"] * t["open_rate"] * (1 + drag) * (1 + buy_fee[i])      # drag (spread/skrid) margfaldast, eins og i fee_research.py
        if i in sell_fee:
            net = t["amount"] * t["close_rate"] * (1 - drag) * (1 - sell_fee[i]) - cost
            closed_net += net
            closed_n += 1
            per_trade.append({"net": net, "cost": cost, "open": False})
        elif t.get("current_rate"):
            sf = _fee(table, now_idx, _side("sell", mode), bnb)                   # tilgátulok i thvi threpi sem gildir NU
            net = t["amount"] * t["current_rate"] * (1 - drag) * (1 - sf) - cost
            unreal += net
            open_n += 1
            per_trade.append({"net": net, "cost": cost, "open": True})
    week_ago = now_ms - 7 * DAY_MS
    # Hradi = velta sidustu 7 daga deilt med DOGUNUM sem gognin na yfir (mest 7, minnst 1) - annars er hradinn vanmetinn thegar sagan er stutt
    span_days = min(7.0, max(1.0, (now_ms - events[0][0]) / DAY_MS)) if events else 7.0
    pace = sum(n for ts, _, _, _, n in events if ts > week_ago) / span_days
    cur = table["tiers"][now_idx]
    nxt = None
    if now_idx + 1 < len(table["tiers"]) and fixed_tier is None:
        n = table["tiers"][now_idx + 1]
        if n["min_volume"] is None:
            nxt = {"name": n["name"], "min_volume": None, "remaining": None, "reachable": None, "eta_days": None}
        else:
            remaining = max(0.0, n["min_volume"] - vol)
            reachable = pace * 30 >= n["min_volume"]
            nxt = {"name": n["name"], "min_volume": n["min_volume"], "remaining": remaining, "reachable": reachable,
                   "eta_days": (remaining / pace) if (reachable and pace > 0) else None}
    return {"tier": cur["name"], "tier_index": now_idx, "volume_30d": vol, "fee_maker": _fee(table, now_idx, "maker", bnb),
            "fee_taker": _fee(table, now_idx, "taker", bnb), "next": nxt, "pace_per_day": pace, "steady_state_30d": pace * 30,
            "timeline": timeline, "closed_n": closed_n, "open_n": open_n, "closed_net": closed_net, "unrealized": unreal,
            "total": closed_net + unreal, "fees_paid": fees_paid, "trades": per_trade}


def analyze(trades, table, now_ms, modes=("taker", "maker"), **kw):
    """Threpa-hermun + samanburdur vid fast lagsta threp, fyrir hvern mode."""
    out = {"missing": missing_thresholds(table), "modes": {}}
    for m in modes:
        tiered = simulate(trades, table, now_ms, mode=m, **kw)
        fixed = simulate(trades, table, now_ms, mode=m, fixed_tier=0, **kw)
        out["modes"][m] = {"tiered": tiered, "fixed": {k: fixed[k] for k in ("tier", "closed_net", "unrealized", "total", "fees_paid")},
                           "savings": tiered["total"] - fixed["total"]}
    return out
# --- tier-engine-end

import time as _time

TIERS_FILE = os.environ.get("TIERS_FILE", "/app/tiers.json")
TIER_START_VOLUME = float(os.environ.get("TIER_START_VOLUME", "0"))
TIER_USE_BNB = os.environ.get("TIER_USE_BNB", "0") == "1"


def _tier_trades(with_meta=False):
    """Öll viðskipti úr Freqtrade á sniði sem threpavélin skilur (ts i millisek.)."""
    allt, offset = [], 0
    for _ in range(40):
        page = ft_get(f"/api/v1/trades?limit=500&offset={offset}")
        got = page.get("trades", [])
        allt += got
        offset += len(got)
        if not got or offset >= page.get("total_trades", 0):
            break
    status = {t.get("trade_id"): t for t in ft_get("/api/v1/status")}
    out = []
    for t in allt:
        if not (t.get("amount") and t.get("open_rate") and t.get("open_timestamp")):
            continue
        is_open = t.get("is_open") is not False
        if not is_open and not (t.get("close_timestamp") and t.get("close_rate")):
            continue
        out.append({
            "open_ts": int(t["open_timestamp"]), "close_ts": None if is_open else int(t["close_timestamp"]),
            "amount": float(t["amount"]), "open_rate": float(t["open_rate"]),
            "close_rate": None if is_open else float(t["close_rate"]),
            "current_rate": (status.get(t.get("trade_id")) or {}).get("current_rate") if is_open else None,
        })
    now_ms = int(_time.time() * 1000)
    if not with_meta:
        return out, now_ms
    closed = [t for t in allt if t.get("is_open") is False and t.get("close_rate")]
    return out, now_ms, {"booked_pnl": sum(float(t.get("close_profit_abs") or 0) for t in closed),
                         "booked_fee_pct": (closed[0].get("fee_open") * 100) if closed and closed[0].get("fee_open") is not None else None}


@app.route("/api/tiers")
def api_tiers():
    """Threpahermir: byrjar i lagsta threpi, velta safnast, gjold laekka. Reiknad ofan a vidskiptin (botninn oraskadur)."""
    try:
        tables, warns = load_tiers(TIERS_FILE)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 500
    try:
        trades, now_ms = _tier_trades()
        out = {"warnings": warns, "now_ms": now_ms, "exchanges": {}}
        for ex, table in tables.items():
            res = analyze(trades, table, now_ms, modes=("taker", "maker"), start_volume=TIER_START_VOLUME,
                          bnb=(TIER_USE_BNB and ex == "binance"))
            out["exchanges"][ex] = {"label": table["label"], "missing": res["missing"], "modes": res["modes"],
                                    "base_tier": table["tiers"][0]["name"]}
        return jsonify(out)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


EXCHANGE_FEES = [
    {"key": "binance", "name": "Binance", "fee": float(os.environ.get("COMPARE_FEE_BINANCE", "0.001"))},
    # Coinbase Advanced, Intro-þrep: maker 0,25% (limit-pöntun sem fyllist), taker 0,50% (markaðspöntun)
    {"key": "coinbase_maker", "name": "Coinbase maker", "fee": float(os.environ.get("COMPARE_FEE_COINBASE_MAKER", "0.0025"))},
    {"key": "coinbase", "name": "Coinbase taker", "fee": float(os.environ.get("COMPARE_FEE_COINBASE", "0.005"))},
]


def _net_profit(amount, open_rate, close_rate, fee):
    """Hagnaður í USDT eftir gjöld á báðum hliðum, og kostnaðarverð (með kaupgjaldi)."""
    cost = amount * open_rate * (1 + fee)
    proceeds = amount * close_rate * (1 - fee)
    return proceeds - cost, cost


def _compare_with_tiers():
    """Binance vs. Coinbase taflan med THREPAGJOLDUM: sama snid og fasta utgafan, en gjald hvers vidskipta er gjaldid i threpinu sem gilti tha."""
    try:
        tables, warns = load_tiers(TIERS_FILE)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 500
    try:
        trades, now_ms, meta = _tier_trades(with_meta=True)
        scenarios = (("binance", "binance", "taker", "Binance"), ("coinbase_maker", "coinbase", "maker", "Coinbase maker"),
                     ("coinbase", "coinbase", "taker", "Coinbase taker"))
        result = []
        for key, ex, mode, name in scenarios:
            sim = simulate(trades, tables[ex], now_ms, mode=mode, start_volume=TIER_START_VOLUME, bnb=(TIER_USE_BNB and ex == "binance"))
            closed = [x for x in sim["trades"] if not x["open"]]
            wins = sum(1 for x in closed if x["net"] > 0)
            pcts = [x["net"] / x["cost"] * 100 for x in closed if x["cost"]]
            result.append({
                "key": key, "name": name, "tier": sim["tier"],
                "fee_pct": (sim["fee_maker"] if mode == "maker" else sim["fee_taker"]) * 100,
                "closed": len(closed), "wins": wins, "losses": len(closed) - wins,
                "winrate": (wins / len(closed) * 100) if closed else None,
                "closed_pnl": sim["closed_net"], "avg_pct": (sum(pcts) / len(pcts)) if pcts else None,
                "open_count": sim["open_n"], "open_pnl": sim["unrealized"], "total_pnl": sim["total"],
            })
        return jsonify({"exchanges": result, "booked_pnl": meta["booked_pnl"], "booked_fee_pct": meta["booked_fee_pct"],
                        "tiers": True, "warnings": warns})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/exchange-compare")
def api_exchange_compare():
    """Endurreiknar sömu viðskipti (sömu kaup- og söluverð) með gjöldum hverrar kauphallar.
    Þetta er EKKI ný hermun: merki og verð eru þau sömu, bara gjöldin breytast.
    ?tiers=1: gjald hvers viðskipta er gjaldið í því þrepi sem gilti þá stundina (botninn byrjar í lægsta þrepi)."""
    if request.args.get("tiers") == "1":
        return _compare_with_tiers()
    try:
        closed, offset = [], 0
        for _ in range(40):  # öryggismörk: mest 40 síður x 500 viðskipti
            page = ft_get(f"/api/v1/trades?limit=500&offset={offset}")
            trades = page.get("trades", [])
            closed += [t for t in trades if t.get("is_open") is False and t.get("close_rate")]
            offset += len(trades)
            if not trades or offset >= page.get("total_trades", 0):
                break

        open_trades = [t for t in ft_get("/api/v1/status") if t.get("current_rate")]

        booked = sum(float(t.get("close_profit_abs") or 0) for t in closed)
        booked_fee = closed[0].get("fee_open") if closed else None

        result = []
        for ex in EXCHANGE_FEES:
            fee = ex["fee"]
            pnl, pcts = [], []
            for t in closed:
                net, cost = _net_profit(t["amount"], t["open_rate"], t["close_rate"], fee)
                pnl.append(net)
                pcts.append(net / cost * 100 if cost else 0.0)
            wins = sum(1 for x in pnl if x > 0)
            unrealized = sum(
                _net_profit(t["amount"], t["open_rate"], t["current_rate"], fee)[0]
                for t in open_trades
                if t.get("amount") and t.get("open_rate")
            )
            result.append({
                "key": ex["key"],
                "name": ex["name"],
                "fee_pct": fee * 100,
                "closed": len(pnl),
                "wins": wins,
                "losses": len(pnl) - wins,
                "winrate": (wins / len(pnl) * 100) if pnl else None,
                "closed_pnl": sum(pnl),
                "avg_pct": (sum(pcts) / len(pcts)) if pcts else None,
                "open_count": len(open_trades),
                "open_pnl": unrealized,
                "total_pnl": sum(pnl) + unrealized,
            })
        return jsonify({
            "exchanges": result,
            "booked_pnl": booked,
            "booked_fee_pct": (booked_fee * 100) if booked_fee is not None else None,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/summary")
def api_summary():
    try:
        balance = ft_get("/api/v1/balance")
        status = ft_get("/api/v1/status")
        return jsonify({
            "total": balance.get("total", 0),
            "currency": balance.get("stake", "USDT"),
            "open_trades": len(status),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/open-trades")
def api_open_trades():
    try:
        status = ft_get("/api/v1/status")
        trades = [
            {
                "pair": t.get("pair"),
                # alltaf PRÓSENTA: profit_ratio * 100 (profit_pct frá Freqtrade er þegar prósenta - ekki margfalda aftur)
                "profit_pct": (t["profit_ratio"] * 100) if t.get("profit_ratio") is not None else (t.get("profit_pct") or 0),
                "open_rate": t.get("open_rate"),
                "current_rate": t.get("current_rate"),
                "open_date": t.get("open_date"),
            }
            for t in status
        ]
        return jsonify(trades)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/closed-trades")
def api_closed_trades():
    try:
        data = ft_get("/api/v1/trades?limit=500")
        closed_all = [t for t in data.get("trades", []) if t.get("is_open") is False]
        closed_all.sort(key=lambda t: t.get("close_timestamp") or t.get("trade_id") or 0, reverse=True)
        data = {"trades": closed_all[:30]}
        trades = [
            {
                "pair": t.get("pair"),
                "profit_pct": t.get("close_profit", t.get("profit_ratio", 0)) * 100
                if t.get("close_profit") is not None or t.get("profit_ratio") is not None else 0,
                "open_rate": t.get("open_rate"),
                "close_rate": t.get("close_rate"),
                "close_date": t.get("close_date"),
                "exit_reason": t.get("exit_reason", "?"),
            }
            for t in data.get("trades", [])
            if t.get("is_open") is False
        ]
        return jsonify(trades)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# --- Binance testnet - kertagraf og pöntunarbók (sömu gögn og Freqtrade sjálft notar) ---

@app.route("/api/symbols")
def api_symbols():
    """Pörin sem botninn er raunverulega með (úr Freqtrade /whitelist). Fellilistinn sýnir þannig aldrei
    annað en það sem botninn verslar með. Ef Freqtrade svarar ekki er notaður fastur varalisti."""
    try:
        wl = ft_get("/api/v1/whitelist").get("whitelist", [])
        syms = [p.split(":")[0].replace("/", "") for p in wl if "/" in p]
        if syms:
            return jsonify({"symbols": syms, "source": "freqtrade"})
    except Exception:
        pass
    return jsonify({"symbols": AVAILABLE_SYMBOLS, "source": "fallback"})


@app.route("/api/ticker/<symbol>")
def api_ticker(symbol):
    try:
        resp = requests.get(f"{MARKET_BASE_URL}/api/v3/ticker/24hr",
                             params={"symbol": symbol.upper()}, timeout=8)
        resp.raise_for_status()
        d = resp.json()
        return jsonify({
            "last_price": float(d["lastPrice"]),
            "price_change_pct": float(d["priceChangePercent"]),
            "high": float(d["highPrice"]),
            "low": float(d["lowPrice"]),
            "volume_quote": float(d["quoteVolume"]),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/klines/<symbol>")
def api_klines(symbol):
    try:
        resp = requests.get(f"{MARKET_BASE_URL}/api/v3/klines",
                             params={"symbol": symbol.upper(), "interval": "1m", "limit": 200},
                             timeout=8)
        resp.raise_for_status()
        raw = resp.json()
        candles = [{"time": int(k[0] / 1000), "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4])} for k in raw]
        volumes = [{"time": int(k[0] / 1000), "value": float(k[5]),
                    "color": "#26a69a80" if float(k[4]) >= float(k[1]) else "#ef535080"} for k in raw]
        return jsonify({"candles": candles, "volumes": volumes})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/depth/<symbol>")
def api_depth(symbol):
    try:
        resp = requests.get(f"{MARKET_BASE_URL}/api/v3/depth",
                             params={"symbol": symbol.upper(), "limit": 15}, timeout=8)
        resp.raise_for_status()
        d = resp.json()

        def fmt(levels):
            out, total = [], 0.0
            for price, qty in levels:
                total += float(qty)
                out.append({"price": float(price), "amount": float(qty), "total": total})
            return out

        return jsonify({"bids": fmt(d["bids"]), "asks": fmt(d["asks"])})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML, symbols=AVAILABLE_SYMBOLS)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="is">
<head>
<meta charset="UTF-8">
<title>Freqtrade PAPER - Mælaborð</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Helvetica Neue", Roboto, sans-serif;
    background: #0a0b0d; color: #e8eaed; margin: 0; font-size: 13px;
  }
  .topbar {
    display: flex; align-items: center; gap: 28px;
    padding: 12px 20px; border-bottom: 1px solid #1e2128; background: #0d0f13;
  }
  .symbol-picker { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 15px; }
  .symbol-picker select { background: transparent; color: #e8eaed; border: none; font-weight: 600; font-size: 15px; cursor: pointer; }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .stat .label { color: #8a8f98; font-size: 11px; }
  .stat .value { font-weight: 600; }
  .price-now { font-size: 15px; font-weight: 700; }
  .up { color: #26a69a; } .down { color: #ef5350; }

  .main { display: flex; }
  .chart-panel { flex: 1; padding: 12px; min-width: 0; }
  .chart-container { border: 1px solid #1e2128; border-radius: 4px; overflow: hidden; }
  #chart { height: 420px; }

  .orderbook-panel { width: 300px; border-left: 1px solid #1e2128; padding: 12px; flex-shrink: 0; }
  .panel-title { font-weight: 600; font-size: 13px; margin-bottom: 8px; display: flex; justify-content: space-between; color: #c7cad1; }
  .ob-header { display: grid; grid-template-columns: 1fr 1fr 1fr; color: #8a8f98; font-size: 11px; padding: 4px 6px; }
  .ob-row { display: grid; grid-template-columns: 1fr 1fr 1fr; padding: 2px 6px; font-size: 12px; position: relative; }
  .ob-row .bg { position: absolute; top: 0; right: 0; bottom: 0; z-index: 0; opacity: 0.15; }
  .ob-row span { position: relative; z-index: 1; }
  .ob-ask .price { color: #ef5350; } .ob-ask .bg { background: #ef5350; }
  .ob-bid .price { color: #26a69a; } .ob-bid .bg { background: #26a69a; }
  .ob-spread { text-align: center; padding: 8px 0; color: #8a8f98; border-top: 1px solid #1e2128; border-bottom: 1px solid #1e2128; margin: 4px 0; font-weight: 600; }

  .bottom { display: flex; border-top: 1px solid #1e2128; }
  .bottom-panel { flex: 1; padding: 12px 20px; }
  .bottom-panel + .bottom-panel { border-left: 1px solid #1e2128; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #16181d; }
  th { color: #8a8f98; font-weight: 500; }
  .pnl-pos { color: #26a69a; font-weight: 600; }
  .pnl-neg { color: #ef5350; font-weight: 600; }
  .compare-panel { padding: 12px 20px; border-bottom: 1px solid #1e2128; background: #0d0f13; }
  .summary-bar { display: flex; gap: 24px; padding: 10px 20px; background: #0d0f13; border-bottom: 1px solid #1e2128; }
</style>
</head>
<body>
<div id="staleBanner" style="display:none;background:#b71c1c;color:#fff;padding:7px 20px;font-size:13px;font-weight:600;"></div>

  <div class="topbar">
    <div class="symbol-picker">
      <span>🔷</span>
      <select id="symbolSelect">
        {% for s in symbols %}
        <option value="{{ s }}">{{ s[:-4] }}-{{ s[-4:] }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="stat"><span class="label">Last Price</span><span class="value price-now" id="lastPrice">--</span></div>
    <div class="stat"><span class="label">Volume (24H)</span><span class="value" id="statVolume">--</span></div>
    <div class="stat"><span class="label">High (24H)</span><span class="value" id="statHigh">--</span></div>
    <div class="stat"><span class="label">Low (24H)</span><span class="value" id="statLow">--</span></div>
  </div>

  <div class="summary-bar">
    <div class="stat"><span class="label">Heildarupphæð (Freqtrade)</span><span class="value" id="totalBalance">--</span></div>
    <div class="stat"><span class="label">Opnar stöður</span><span class="value" id="openCount">--</span></div>
    <div class="stat"><span class="label">Hagnaður (lokuð viðskipti)</span><span class="value" id="perfClosed">--</span></div>
    <div class="stat"><span class="label">Hagnaður (með opnum stöðum)</span><span class="value" id="perfAll">--</span></div>
    <div class="stat"><span class="label">Sigurhlutfall</span><span class="value" id="perfWinrate">--</span></div>
    <div class="stat"><span class="label">Lokuð viðskipti (unnin / töpuð)</span><span class="value" id="perfTrades">--</span></div>
    <div class="stat"><span class="label">Besta mynt</span><span class="value" id="perfBest">--</span></div>
  </div>

  <div class="compare-panel">
    <div class="panel-title"><span>Binance vs. Coinbase &mdash; sömu viðskipti, ólík gjöld</span><span><button id="cmpFixedBtn" onclick="setCmpMode('fixed')" style="background:#26a69a;color:#0b0d10;border:0;border-radius:4px;padding:2px 10px;cursor:pointer;font-weight:600;">Fast gjald</button> <button id="cmpTierBtn" onclick="setCmpMode('tiers')" style="background:#1e2128;color:#d1d4dc;border:0;border-radius:4px;padding:2px 10px;cursor:pointer;font-weight:600;">Með þrepum</button></span><span id="cmpNote" style="color:#8a8f98;font-weight:400;"></span></div>
    <table>
      <thead><tr><th></th><th id="cmpHeadBinance">Binance</th><th id="cmpHeadMaker">Coinbase maker</th><th id="cmpHeadCoinbase">Coinbase taker</th><th>Munur (taker − Binance)</th></tr></thead>
      <tbody id="cmpBody"><tr><td colspan="5" style="color:#8a8f98;">Sæki gögn...</td></tr></tbody>
    </table>
    <div style="color:#8a8f98;font-size:11px;margin-top:6px;">Sömu kaup- og söluverð endurreiknuð með gjöldum á báðar hliðar. Ekki ný hermun: merki og verð eru óbreytt. Maker er BESTA tilvik (allar limit-pantanir fyllast, líka stop-loss sem í raun er taker); raunveruleg útkoma er einhvers staðar milli maker og taker. <span id="cmpModeNote" style="color:#ffb74d;"></span></div>
  </div>

  <div class="compare-panel" id="tierPanel">
    <div class="panel-title"><span>Gjaldaþrep (hermt yfirlag) &mdash; botninn byrjar í lægsta þrepi og vinnur sig upp</span><span style="color:#8a8f98;font-weight:400;">viðskiptin sjálf eru óbreytt</span></div>
    <div id="tierBody" style="display:flex;gap:16px;flex-wrap:wrap;color:#8a8f98;">Sæki gögn...</div>
    <div style="color:#8a8f98;font-size:11px;margin-top:6px;">Velta = kaup + sala (USD) síðustu 30 daga. Gjald hvers viðskipta er gjaldið í því þrepi sem gilti þá stundina. Coinbase-þröskuldar koma úr tiers.json; Binance gerir ráð fyrir að BNB-skilyrðið sé uppfyllt. Áætlun, ekki raunveruleg gjöld.</div>
  </div>

  <div class="main">
    <div class="chart-panel">
      <div class="chart-container"><div id="chart"></div></div>
    </div>
    <div class="orderbook-panel">
      <div class="panel-title"><span>Order book</span></div>
      <div class="ob-header"><span>Price (USDT)</span><span style="text-align:right">Amount</span><span style="text-align:right">Total</span></div>
      <div id="asksBody"></div>
      <div class="ob-spread" id="spreadDisplay">--</div>
      <div id="bidsBody"></div>
    </div>
  </div>

  <div class="bottom">
    <div class="bottom-panel">
      <div class="panel-title"><span>Opnar stöður (Freqtrade)</span></div>
      <div style="max-height:340px;overflow-y:auto;">
      <table>
        <thead><tr><th>Mynt</th><th>Kaupverð → Núverandi</th><th>Hagnaður/Tap</th></tr></thead>
        <tbody id="openTradesBody"><tr><td colspan="3" style="color:#8a8f98;">Engar opnar stöður...</td></tr></tbody>
      </table>
      </div>
    </div>
    <div class="bottom-panel">
      <div class="panel-title"><span>Lokuð viðskipti (Freqtrade)</span></div>
      <div style="max-height:340px;overflow-y:auto;">
      <table>
        <thead><tr><th>Mynt</th><th>Ástæða</th><th>Verð</th><th>Hagnaður/Tap</th></tr></thead>
        <tbody id="closedTradesBody"><tr><td colspan="4" style="color:#8a8f98;">Engin lokuð viðskipti ennþá...</td></tr></tbody>
      </table>
      </div>
    </div>
  </div>

<script>
let chart, candleSeries, volumeSeries;
let currentSymbol = document.getElementById('symbolSelect').value;

function initChart() {
  const el = document.getElementById('chart');
  chart = LightweightCharts.createChart(el, {
    layout: { background: { color: '#0a0b0d' }, textColor: '#8a8f98' },
    grid: { vertLines: { color: '#16181d' }, horzLines: { color: '#16181d' } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#1e2128' },
    rightPriceScale: { borderColor: '#1e2128' },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    width: el.clientWidth, height: 420,
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350', priceScaleId: 'right',
  });
  candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.3 } });
  volumeSeries = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
  new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth })).observe(el);
}

async function loadKlines() {
  const res = await fetch(`/api/klines/${currentSymbol}`);
  const data = await res.json();
  if (data.error) return;
  candleSeries.setData(data.candles);
  volumeSeries.setData(data.volumes);
  chart.timeScale().fitContent();
}

// --- stale-start
let lastTickerOk = Date.now();
let lastTickerAt = new Date().toLocaleTimeString('is-IS');
function setStale(isStale) {
  const b = document.getElementById('staleBanner');
  const p = document.getElementById('lastPrice');
  if (isStale) {
    b.textContent = 'Engin ný gögn síðan ' + lastTickerAt + ' - gangan til netþjónsins eða markaðsgögnin svara ekki. Verðið sem sést er gamalt.';
    b.style.display = 'block';
    p.style.opacity = '0.35';
  } else {
    b.style.display = 'none';
    p.style.opacity = '1';
  }
}
function checkStale() { if (Date.now() - lastTickerOk > 15000) setStale(true); }
async function loadTicker() {
  try {
    const res = await fetch(`/api/ticker/${currentSymbol}`);
    const d = await res.json();
    if (d.error) throw new Error(d.error);
    const priceEl = document.getElementById('lastPrice');
    const cls = d.price_change_pct >= 0 ? 'up' : 'down';
    const sign = d.price_change_pct >= 0 ? '+' : '';
    priceEl.innerHTML = `$${d.last_price.toLocaleString('en-US', {maximumFractionDigits: 6})} <span class="${cls}" style="font-size:12px">${sign}${d.price_change_pct.toFixed(2)}%</span>`;
    document.getElementById('statVolume').textContent = '$' + (d.volume_quote / 1e6).toFixed(2) + 'M';
    document.getElementById('statHigh').textContent = '$' + d.high.toLocaleString('en-US', {maximumFractionDigits: 6});
    document.getElementById('statLow').textContent = '$' + d.low.toLocaleString('en-US', {maximumFractionDigits: 6});
    lastTickerOk = Date.now();
    lastTickerAt = new Date().toLocaleTimeString('is-IS');
    setStale(false);
  } catch (e) {
    checkStale();
  }
}
setInterval(checkStale, 3000);
// --- stale-end

function renderBookSide(container, rows, side) {
  const maxTotal = Math.max(...rows.map(r => r.total), 1);
  container.innerHTML = rows.map(r => {
    const pct = (r.total / maxTotal) * 100;
    return `<div class="ob-row ob-${side}"><div class="bg" style="width:${pct}%"></div>
      <span class="price">${r.price.toFixed(6)}</span>
      <span style="text-align:right">${r.amount.toFixed(3)}</span>
      <span style="text-align:right">${r.total.toFixed(3)}</span></div>`;
  }).join('');
}

async function loadDepth() {
  const res = await fetch(`/api/depth/${currentSymbol}`);
  const d = await res.json();
  if (d.error) return;
  const asks = d.asks.slice(0, 10).reverse();
  const bids = d.bids.slice(0, 10);
  renderBookSide(document.getElementById('asksBody'), asks, 'ask');
  renderBookSide(document.getElementById('bidsBody'), bids, 'bid');
  if (asks.length && bids.length) {
    document.getElementById('spreadDisplay').textContent =
      `${d.bids[0].price.toFixed(6)} · Spread ${(d.asks[0].price - d.bids[0].price).toFixed(6)}`;
  }
}

async function loadSummary() {
  const res = await fetch('/api/summary');
  const d = await res.json();
  if (d.error) {
    document.getElementById('totalBalance').textContent = 'Villa: ' + d.error;
    return;
  }
  document.getElementById('totalBalance').textContent = d.total.toLocaleString('is-IS', {maximumFractionDigits: 2}) + ' ' + d.currency;
  document.getElementById('openCount').textContent = d.open_trades;
}

async function loadPerformance() {
  const res = await fetch('/api/performance');
  const d = await res.json();
  if (d.error) return;

  const fmtPct = (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  const colorize = (el, v) => { el.className = 'value ' + (v >= 0 ? 'up' : 'down'); };

  const closedEl = document.getElementById('perfClosed');
  closedEl.textContent = fmtPct(d.profit_closed_pct) + ' (' + d.profit_closed_coin.toFixed(2) + ' USDT)';
  colorize(closedEl, d.profit_closed_pct);

  const allEl = document.getElementById('perfAll');
  allEl.textContent = fmtPct(d.profit_all_pct) + ' (' + d.profit_all_coin.toFixed(2) + ' USDT)';
  colorize(allEl, d.profit_all_pct);

  const decided = d.winning_trades + d.losing_trades;
  const winrate = decided > 0 ? (d.winning_trades / decided) * 100 : 0;
  document.getElementById('perfWinrate').textContent = decided > 0 ? winrate.toFixed(1) + '%' : 'Engin lokuð viðskipti ennþá';
  document.getElementById('perfTrades').textContent = d.winning_trades + ' / ' + d.losing_trades + ' (af ' + d.closed_trade_count + ' lokuðum)';
  document.getElementById('perfBest').textContent = d.best_pair + ' (' + fmtPct(d.best_pair_profit_pct) + ')';
}

// --- tiers-start
function fmtUsd(x) {
  if (x === null || x === undefined) return '?';
  if (x >= 1e9) return '$' + (x / 1e9).toFixed(1) + ' ma.';
  if (x >= 1e6) return '$' + (x / 1e6).toFixed(1) + ' m.';
  if (x >= 1e3) return '$' + (x / 1e3).toFixed(1) + ' þ.';
  return '$' + x.toFixed(0);
}
async function loadTiers() {
  try {
    const res = await fetch('/api/tiers');
    const d = await res.json();
    const box = document.getElementById('tierBody');
    if (d.error) { box.innerHTML = '<div style="color:#ef5350;">Villa: ' + d.error + '</div>'; return; }
    const sgn = (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + ' USDT';
    const colr = (v) => v >= 0 ? '#26a69a' : '#ef5350';
    box.innerHTML = Object.values(d.exchanges).map(ex => {
      const t = ex.modes.taker.tiered, nx = t.next;
      let h = '<div style="min-width:420px;flex:1;border:1px solid #1e2128;border-radius:6px;padding:10px 12px;color:#d1d4dc;">';
      h += '<div style="font-weight:700;margin-bottom:4px;">' + ex.label + '</div>';
      h += '<div>Þrep: <b>' + t.tier + '</b> &nbsp;|&nbsp; velta 30 d.: <b>' + fmtUsd(t.volume_30d) + '</b> &nbsp;|&nbsp; gjald núna: maker ' + (t.fee_maker * 100).toFixed(3) + '% / taker ' + (t.fee_taker * 100).toFixed(3) + '%</div>';
      if (ex.missing.length) {
        h += '<div style="color:#ffb74d;margin-top:4px;">Þröskuldar vantar fyrir ' + ex.missing.length + ' þrep (' + ex.missing.slice(0, 3).join(', ') + (ex.missing.length > 3 ? '...' : '') + '). Fylltu þá inn í tiers.json.</div>';
      }
      if (nx) {
        if (nx.min_volume === null) {
          h += '<div style="margin-top:4px;">Næsta þrep: <b>' + nx.name + '</b> (þröskuldur óþekktur)</div>';
        } else {
          const p = Math.max(0, Math.min(100, t.volume_30d / nx.min_volume * 100));
          h += '<div style="margin-top:6px;">Næsta þrep: <b>' + nx.name + '</b> við ' + fmtUsd(nx.min_volume) + ' &mdash; vantar ' + fmtUsd(nx.remaining) + '</div>';
          h += '<div style="background:#1e2128;height:8px;border-radius:4px;margin:4px 0;"><div style="background:#26a69a;height:8px;border-radius:4px;width:' + p.toFixed(1) + '%;"></div></div>';
          if (nx.reachable === false) h += '<div style="color:#ffb74d;">Næst ekki á núverandi hraða: ' + fmtUsd(t.pace_per_day) + '/dag gefur ' + fmtUsd(t.steady_state_30d) + ' á 30 d.</div>';
          else if (nx.eta_days !== null) h += '<div>Á núverandi hraða (' + fmtUsd(t.pace_per_day) + '/dag): eftir um ' + nx.eta_days.toFixed(1) + ' daga</div>';
        }
      } else { h += '<div style="margin-top:4px;">Hæsta þrep náð.</div>'; }
      ['taker', 'maker'].forEach(m => {
        const r = ex.modes[m];
        h += '<div style="margin-top:6px;">' + m + ': með þrepagjöldum <b style="color:' + colr(r.tiered.total) + '">' + sgn(r.tiered.total) + '</b> &nbsp;|&nbsp; fast ' + ex.base_tier + ' ' + sgn(r.fixed.total) + ' &nbsp;|&nbsp; sparnaður <b style="color:' + colr(r.savings) + '">' + sgn(r.savings) + '</b></div>';
      });
      if (t.timeline.length) {
        h += '<div style="margin-top:6px;color:#8a8f98;">Þrepabreytingar: ' + t.timeline.slice(-4).map(e => new Date(e.ts).toLocaleDateString('is-IS') + ' ' + e.from + ' → ' + e.tier).join(' · ') + '</div>';
      }
      return h + '</div>';
    }).join('');
  } catch (e) { console.error(e); }
}
// --- tiers-end

let cmpMode = 'fixed';
function setCmpMode(m) {
  cmpMode = m;
  const on = 'background:#26a69a;color:#0b0d10;', off = 'background:#1e2128;color:#d1d4dc;';
  const base = 'border:0;border-radius:4px;padding:2px 10px;cursor:pointer;font-weight:600;';
  document.getElementById('cmpFixedBtn').style.cssText = (m === 'fixed' ? on : off) + base;
  document.getElementById('cmpTierBtn').style.cssText = (m === 'tiers' ? on : off) + base;
  loadExchangeCompare();
}

async function loadExchangeCompare() {
  try {
    const res = await fetch('/api/exchange-compare' + (cmpMode === 'tiers' ? '?tiers=1' : ''));
    const d = await res.json();
    const body = document.getElementById('cmpBody');
    if (d.error) { body.innerHTML = '<tr><td colspan="5" style="color:#ef5350;">Villa: ' + d.error + '</td></tr>'; return; }
    const find = (k) => d.exchanges.find(e => e.key === k);
    const ex = [find('binance'), find('coinbase_maker'), find('coinbase')].filter(Boolean);
    const heads = ['cmpHeadBinance', 'cmpHeadMaker', 'cmpHeadCoinbase'];
    const names = ['Binance', 'Coinbase maker', 'Coinbase taker'];
    ex.forEach((e, i) => { document.getElementById(heads[i]).textContent = names[i] + (e.tier ? ' · ' + e.tier : '') + ' (' + e.fee_pct.toFixed(2) + '%)'; });
    document.getElementById('cmpModeNote').textContent = (cmpMode === 'tiers') ? 'MEÐ ÞREPUM: gjald hvers viðskipta er gjaldið í því þrepi sem gilti þá stundina; fyrirsagnir sýna núverandi þrep (sjá spjaldið fyrir neðan).' : '';
    if (d.booked_fee_pct !== null) {
      document.getElementById('cmpNote').textContent = 'Botninn skráði sjálfur: ' + d.booked_pnl.toFixed(2) + ' USDT (gjald ' + d.booked_fee_pct.toFixed(2) + '%)';
    }
    const money = (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + ' USDT';
    const cls = (v) => v >= 0 ? 'pnl-pos' : 'pnl-neg';
    const pct = (v) => v === null ? '-' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
    const wr = (v) => v === null ? '-' : v.toFixed(1) + '%';
    const b = ex[0], c = ex[ex.length - 1];
    const rows = [
      ['Lokuð viðskipti', e => e.closed, null],
      ['Unnin / töpuð', e => e.wins + ' / ' + e.losses, null],
      ['Sigurhlutfall', e => wr(e.winrate), (b.winrate !== null) ? (c.winrate - b.winrate).toFixed(1) + ' pp' : '-'],
      ['Meðaltal á viðskipti', e => pct(e.avg_pct), (b.avg_pct !== null) ? (c.avg_pct - b.avg_pct).toFixed(2) + ' pp' : '-'],
      ['Hagnaður/tap, lokuð', e => money(e.closed_pnl), money(c.closed_pnl - b.closed_pnl), e => e.closed_pnl],
      ['Fljótandi, ' + b.open_count + ' opnar stöður', e => money(e.open_pnl), money(c.open_pnl - b.open_pnl), e => e.open_pnl],
      ['Samtals', e => money(e.total_pnl), money(c.total_pnl - b.total_pnl), e => e.total_pnl],
    ];
    body.innerHTML = rows.map((r, i) => {
      const colorOf = r[3];
      const cells = ex.map(e => '<td' + (colorOf ? ' class="' + cls(colorOf(e)) + '"' : '') + '>' + r[1](e) + '</td>').join('');
      const bold = i === 6 ? ' style="font-weight:700;"' : '';
      return '<tr' + bold + '><td>' + r[0] + '</td>' + cells + '<td style="color:#8a8f98;">' + (r[2] === null ? '' : r[2]) + '</td></tr>';
    }).join('');
  } catch (e) { console.error(e); }
}

function fmtPrice(x) {
  if (x === null || x === undefined || isNaN(x)) return '-';
  const a = Math.abs(x);
  return a >= 100 ? x.toFixed(2) : a >= 1 ? x.toFixed(4) : x.toPrecision(4);
}

async function loadOpenTrades() {
  const res = await fetch('/api/open-trades');
  const trades = await res.json();
  const body = document.getElementById('openTradesBody');
  if (!Array.isArray(trades) || !trades.length) {
    body.innerHTML = '<tr><td colspan="3" style="color:#8a8f98;">Engar opnar stöður...</td></tr>';
    return;
  }
  body.innerHTML = trades.map(t => {
    const cls = t.profit_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
    return `<tr><td>${t.pair}</td><td>${fmtPrice(t.open_rate)} → ${fmtPrice(t.current_rate)}</td>
      <td class="${cls}">${Number(t.profit_pct).toFixed(2)}%</td></tr>`;
  }).join('');
}

async function loadClosedTrades() {
  const res = await fetch('/api/closed-trades');
  const trades = await res.json();
  const body = document.getElementById('closedTradesBody');
  if (!Array.isArray(trades) || !trades.length) {
    body.innerHTML = '<tr><td colspan="4" style="color:#8a8f98;">Engin lokuð viðskipti ennþá...</td></tr>';
    return;
  }
  body.innerHTML = trades.map(t => {
    const cls = t.profit_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
    return `<tr><td>${t.pair}</td><td>${t.exit_reason}</td>
      <td>${fmtPrice(t.open_rate)} → ${fmtPrice(t.close_rate)}</td>
      <td class="${cls}">${t.profit_pct >= 0 ? '+' : ''}${t.profit_pct.toFixed(2)}%</td></tr>`;
  }).join('');
}

// --- symbols-start
async function loadSymbols() {
  try {
    const res = await fetch('/api/symbols');
    const d = await res.json();
    if (!d.symbols || !d.symbols.length) return;
    const sel = document.getElementById('symbolSelect');
    const keep = d.symbols.includes(currentSymbol) ? currentSymbol : d.symbols[0];
    sel.innerHTML = d.symbols.map(s => `<option value="${s}">${s.slice(0, -4)}-${s.slice(-4)}</option>`).join('');
    sel.value = keep;
    currentSymbol = keep;
  } catch (e) { console.error(e); }
}
// --- symbols-end

document.getElementById('symbolSelect').addEventListener('change', (e) => {
  currentSymbol = e.target.value;
  loadKlines();
  loadTicker();
  loadDepth();
});

initChart();
loadSymbols().then(() => { loadKlines(); loadTicker(); loadDepth(); });
setInterval(loadSymbols, 60000);
loadSummary();
loadPerformance();
loadExchangeCompare();
loadTiers();
loadOpenTrades();
loadClosedTrades();

setInterval(loadTicker, 2000);
setInterval(loadDepth, 2000);
setInterval(loadSummary, 3000);
setInterval(loadPerformance, 10000);
setInterval(loadExchangeCompare, 10000);
setInterval(loadTiers, 60000);
setInterval(loadOpenTrades, 3000);
setInterval(loadClosedTrades, 8000);
setInterval(loadKlines, 30000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "5051"))
    print("Innskráning á Freqtrade API...")
    try:
        login()
        print("Innskráning tókst.")
    except Exception as exc:
        # Freqtrade er kannski ekki tilbúið (t.d. rétt eftir endurræsingu) - hver beiðni reynir aftur sjálf.
        print(f"Gat ekki skráð inn strax ({exc}) - reyni aftur sjálfkrafa.")
    print(f"Mælaborð keyrir á http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
