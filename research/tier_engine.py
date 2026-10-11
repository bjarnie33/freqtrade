# -*- coding: utf-8 -*-
"""
tier_engine.py - threpahermir fyrir kauphallir (Coinbase, Binance). Hrein Python, engin utanaðkomandi söfn.

Botninn byrjar i LAGSTA threpi. Hver kaup og hver sala (bædi hlidar) telja til veltu (USD). Velta = rullandi 30 daga gluggi.
Threp = haesta threp sem veltan nær (miðað við veltu ÁÐUR en kaupin eru gerð). Gjald hverrar hliðar er gjaldið i threpinu a thvi augnabliki.
Samanburdur: sama hermun med FASTA lagsta threpinu (Intro/Regular) -> hvad sparast i raun.

ATH: threpin og thröskuldar eru ÁÆTLUN. Coinbase-thröskulda ($ velta per threp) verdur notandi ad fylla inn (tiers.json) af
"Tier requirements" flipa Coinbase. Binance: BNB-skilyrdi er gert rad fyrir ad se uppfyllt (BNB-stada er ekki hermd).
"""
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
