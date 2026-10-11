# -*- coding: utf-8 -*-
"""
shadow_advisor.py - SKUGGASTILLING: gervigreind radleggur um kaup, botninn hunsar svarid, vid MAELUM hvort radgjofin hefdi hjalpad.

Hvernig:
  * Les NYJU vidskiptin ur paper_trades.sqlite (OPNAD READ-ONLY - thjonustan skrifar aldrei i gagnagrunna botnsins).
  * Fyrir hvert nytt kaup (opnad sidustu minutur) tekur hun visana ur paper_audit.sqlite a KERTINUM sem gaf merkid
    (aldrei eftir kaupin) og spyr Claude: approve eda veto. Svarid er skrad i shadow.sqlite.
  * Botninn kaupir eins og adur. Adeins skyrslan (--report) ber saman utkomu "samthykkt" vs "hafnad" eftir gjold.
  * Engin sagu-baksetning: adeins LIFANDI akvardanir (kaup eldri en SHADOW_MAX_AGE_MIN faer enga akvordun).

Nota:   python shadow_advisor.py            # lykkja (keyrsla i gami)
        python shadow_advisor.py --once     # ein umferd (profun)
        python shadow_advisor.py --report   # skyrsla: baetir radgjofin?
Lyklar: ANTHROPIC_API_KEY kemur ur umhverfi (.env a netthjoninum) - aldrei skrifadur i log.
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

PROMPT_VERSION = "v1"
SYSTEM_PROMPT = (
    "You are a risk filter for an automated mean-reversion crypto strategy running in paper-trading mode. "
    "The strategy buys a USDT spot pair when RSI(14) on 5-minute candles falls below 30, and sells when RSI rises above 70 "
    "or a -5% stop-loss triggers. Round-trip trading costs are between 0.2% and 1.0% depending on the exchange, so an entry "
    "is only worth taking if the price is likely to rebound by more than about 1% before the strategy exits. "
    "You receive the indicator values of the candle that produced the signal. Use ONLY that data and your general knowledge; "
    "do not assume anything about future prices. Decide whether to approve the entry or veto it (veto when the data suggests "
    "the decline is likely to continue or the setup is poor). "
    'Reply with ONLY a JSON object, no other text: {"decision": "approve" or "veto", "confidence": a number from 0 to 1, '
    '"reason": a short explanation under 200 characters}.'
)
PROMPT_HASH = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]

# Eina sem fer til gervigreindarinnar um hvert kaup (hvitlisti - engar utkomu- eda framtidarupplysingar):
FEATURE_DEFS = [
    ("rsi_14", "RSI(14)"),
    ("return_1", "price change over the last 5-minute candle (fraction)"),
    ("return_12", "price change over the last 12 candles = 1 hour (fraction)"),
    ("volume_ratio", "volume divided by the mean volume of the last 12 candles"),
    ("volatility_12", "standard deviation of 1-candle returns over the last 12 candles"),
    ("sma_ratio", "close divided by its 24-candle simple moving average, minus 1"),
    ("macd_ratio", "(EMA12 - EMA26) divided by close"),
]
FEATURE_KEYS = [k for k, _ in FEATURE_DEFS]
FEES = {"Binance 0.1%": 0.001, "Coinbase maker 0.25%": 0.0025, "Coinbase taker 0.5%": 0.005}

SCHEMA = """CREATE TABLE IF NOT EXISTS shadow_decisions(
  trade_id INTEGER PRIMARY KEY, pair TEXT NOT NULL, open_date TEXT NOT NULL, decided_at TEXT NOT NULL,
  model TEXT, prompt_version TEXT, prompt_hash TEXT, features TEXT,
  decision TEXT, confidence REAL, reason TEXT,
  input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER, error TEXT)"""


# ------------------------------------------------------------------ almennt
def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print("%s %s" % (now_utc().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def redact(text, key):
    text = str(text)
    return text.replace(key, "***") if key else text


def parse_ft_date(s):
    dt = datetime.fromisoformat(str(s).replace("T", " "))
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)


def ro_connect(path):
    """Opnar gagnagrunn READ-ONLY: thjonustan getur ekki skrifad i gagnagrunna botnsins."""
    return sqlite3.connect("file:%s?mode=ro" % quote(path), uri=True, timeout=30)


def open_state(path):
    con = sqlite3.connect(path, timeout=30)
    con.execute(SCHEMA)
    con.commit()
    return con


# ------------------------------------------------------------------ gogn ur botni
def read_new_trades(trades_db, state, now, max_age_min):
    """Kaup opnud sidustu max_age_min minutur sem hafa ENGA akvordun. Skilar (trades, fjoldi opinna stada nuna)."""
    cutoff = (now - timedelta(minutes=max_age_min)).strftime("%Y-%m-%d %H:%M:%S")
    con = ro_connect(trades_db)
    try:
        rows = con.execute("SELECT id, pair, open_date, open_rate FROM trades WHERE open_date >= ? ORDER BY open_date", (cutoff,)).fetchall()
        open_n = con.execute("SELECT COUNT(*) FROM trades WHERE is_open = 1").fetchone()[0]
    finally:
        con.close()
    known = {r[0] for r in state.execute("SELECT trade_id FROM shadow_decisions")}
    return [{"id": r[0], "pair": r[1], "open_date": r[2], "open_rate": r[3]} for r in rows if r[0] not in known], open_n


def _candle_features(payload):
    p = json.loads(payload)
    out = {}
    for k in FEATURE_KEYS:
        v = p.get(k)
        if v is None or not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return None
        out[k] = float(v)
    return out


def features_at(audit_db, pair, open_dt, window_min=15):
    """Visar KERTISINS sem gaf merkid: sidasti kerti med dagsetningu <= opnunartima kaupsins (aldrei kerti EFTIR kaupin)."""
    hi = open_dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    lo = (open_dt - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S+00:00")
    con = ro_connect(audit_db)
    try:
        row = con.execute("SELECT date, payload FROM candles WHERE pair = ? AND date <= ? AND date >= ? ORDER BY date DESC LIMIT 1", (pair, hi, lo)).fetchone()
        if row is None:
            return None
        feats = _candle_features(row[1])
        if feats is None:
            return None
        btc = con.execute("SELECT payload FROM candles WHERE pair = 'BTC/USDT' AND date = ?", (row[0],)).fetchone()
        btc_ret = None
        if btc is not None:
            v = json.loads(btc[0]).get("return_12")
            btc_ret = float(v) if isinstance(v, (int, float)) and math.isfinite(v) else None
        return {"candle_date": row[0], "features": feats, "btc_return_12": btc_ret}
    finally:
        con.close()


def build_prompt(pair, open_dt, feats, btc_return_12, open_positions):
    lines = ["pair: %s" % pair, "time_utc: %s (hour %d, %s)" % (open_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), open_dt.hour, open_dt.strftime("%A")),
             "candle: 5 minutes", "indicators at the signal candle:"]
    for k, desc in FEATURE_DEFS:
        lines.append("  %s (%s): %.6g" % (k, desc, feats[k]))
    lines.append("market context:")
    lines.append("  btc_return_12 (BTC/USDT price change over the last hour): %s" % ("%.6g" % btc_return_12 if btc_return_12 is not None else "unknown"))
    lines.append("  positions currently open in the portfolio: %d" % open_positions)
    return "\n".join(lines)


# ------------------------------------------------------------------ Claude-kall
class LLMError(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %s: %s" % (status, str(body)[:200]))
        self.status = status


def make_llm(cfg):
    import requests
    sess = requests.Session()
    url = cfg["base_url"].rstrip("/") + "/v1/messages"
    headers = {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def call(system, user):
        body = {"model": cfg["model"], "max_tokens": 220, "system": system, "messages": [{"role": "user", "content": user}]}
        if cfg.get("temperature") is not None:
            body["temperature"] = cfg["temperature"]
        last = None
        for attempt in range(3):
            t0 = time.time()
            try:
                r = sess.post(url, headers=headers, data=json.dumps(body), timeout=cfg["timeout"])
            except requests.RequestException as exc:
                last = LLMError("connection", redact(exc, cfg["api_key"]))
            else:
                if r.status_code == 200:
                    data = r.json()
                    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                    usage = data.get("usage") or {}
                    return text, usage, int((time.time() - t0) * 1000)
                last = LLMError(r.status_code, redact(r.text, cfg["api_key"]))
                if r.status_code not in (429, 500, 502, 503, 504, 529):
                    raise last                                            # villa sem endurtekning lagar ekki (401, 400, 404 ...)
                wait = min(30.0, float(r.headers.get("retry-after", 2 ** attempt)))
                time.sleep(min(wait, cfg.get("retry_sleep_max", 30.0)))
                continue
            time.sleep(min(2 ** attempt, cfg.get("retry_sleep_max", 30.0)))
        raise last

    return call


def parse_decision(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    decision = str(obj.get("decision", "")).strip().lower()
    if decision not in ("approve", "veto"):
        raise ValueError("decision must be approve or veto, got %r" % decision)
    try:
        conf = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    if math.isnan(conf):
        conf = 0.5
    return {"decision": decision, "confidence": min(1.0, max(0.0, conf)), "reason": str(obj.get("reason", ""))[:240]}


# ------------------------------------------------------------------ akvordanalota
def decisions_today(state, now):
    return state.execute("SELECT COUNT(*) FROM shadow_decisions WHERE model IS NOT NULL AND substr(decided_at, 1, 10) = ?",
                         (now.strftime("%Y-%m-%d"),)).fetchone()[0]


def _insert(state, now, trade, **kw):
    cols = {"trade_id": trade["id"], "pair": trade["pair"], "open_date": str(trade["open_date"]), "decided_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    cols.update(kw)
    state.execute("INSERT OR IGNORE INTO shadow_decisions(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols))), tuple(cols.values()))
    state.commit()


def decide_once(cfg, state, now, llm):
    """Ein umferd: akvordun fyrir hvert nytt kaup. Skilar (nyjar akvardanir, villur)."""
    trades, open_positions = read_new_trades(cfg["trades_db"], state, now, cfg["max_age_min"])
    made = errors = 0
    for t in trades:
        if decisions_today(state, now) >= cfg["max_per_day"]:
            log("daglegt hamark (%d kall) naad - sleppi ad sinni" % cfg["max_per_day"])
            break
        open_dt = parse_ft_date(t["open_date"])
        ctx = features_at(cfg["audit_db"], t["pair"], open_dt)
        if ctx is None:
            _insert(state, now, t, error="no_features")
            continue
        user = build_prompt(t["pair"], open_dt, ctx["features"], ctx["btc_return_12"], open_positions)
        base = {"model": cfg["model"], "prompt_version": PROMPT_VERSION, "prompt_hash": PROMPT_HASH,
                "features": json.dumps({"candle_date": ctx["candle_date"], **ctx["features"], "btc_return_12": ctx["btc_return_12"], "open_positions": open_positions})}
        try:
            text, usage, ms = llm(SYSTEM_PROMPT, user)
        except Exception as exc:
            errors += 1
            _insert(state, now, t, error=redact(exc, cfg.get("api_key", ""))[:200], **base)
            log("Claude-kall mistokst fyrir kaup %s: %s" % (t["id"], redact(exc, cfg.get("api_key", ""))[:120]))
            if getattr(exc, "status", None) in (401, 403):
                raise                                                  # rangur lykill: hættum, ekki halda afram ad reyna
            continue
        base.update({"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "latency_ms": ms})
        try:
            d = parse_decision(text)
        except ValueError as exc:
            errors += 1
            _insert(state, now, t, error=("bad_output: %s" % exc)[:200], **base)
            continue
        _insert(state, now, t, decision=d["decision"], confidence=d["confidence"], reason=d["reason"], **base)
        made += 1
        log("kaup %s %s -> %s (%.2f) %s" % (t["id"], t["pair"], d["decision"], d["confidence"], d["reason"][:80]))
    return made, errors


def load_config():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {
        "api_key": key, "model": os.environ.get("SHADOW_MODEL", "claude-haiku-5-5"),
        "base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        "trades_db": os.environ.get("TRADES_DB", "/ft/paper_trades.sqlite"), "audit_db": os.environ.get("AUDIT_DB", "/ft/paper_audit.sqlite"),
        "state_db": os.environ.get("SHADOW_DB", "/data/shadow.sqlite"),
        "poll": float(os.environ.get("SHADOW_POLL_SECONDS", "30")), "max_age_min": float(os.environ.get("SHADOW_MAX_AGE_MIN", "10")),
        "max_per_day": int(os.environ.get("SHADOW_MAX_PER_DAY", "300")), "timeout": float(os.environ.get("SHADOW_TIMEOUT", "20")),
        "temperature": float(os.environ["SHADOW_TEMPERATURE"]) if os.environ.get("SHADOW_TEMPERATURE") else None,
    }


def run_forever(cfg, llm=None, max_loops=None, sleep=time.sleep, clock=now_utc):
    state = open_state(cfg["state_db"])
    llm = llm or make_llm(cfg)
    log("skuggastilling i gangi: model=%s prompt=%s/%s hamark=%d/dag" % (cfg["model"], PROMPT_VERSION, PROMPT_HASH, cfg["max_per_day"]))
    loops, fails = 0, 0
    while max_loops is None or loops < max_loops:
        loops += 1
        try:
            made, errors = decide_once(cfg, state, clock(), llm)
            fails = fails + 1 if (errors and not made) else 0
        except LLMError as exc:
            if exc.status in (401, 403):
                log("LYKILL HAFNAD (HTTP %s) - bid 10 min. og reyni aftur. Athugadu ANTHROPIC_API_KEY i .env" % exc.status)
                sleep(600)
                continue
            fails += 1
        except sqlite3.Error as exc:
            log("gagnagrunnsvilla (botnagrunnur opinn/laestur?): %s" % exc)
            fails += 1
        except Exception as exc:                                       # thjonustan ma aldrei detta ut
            log("ovaentt villa: %s" % redact(exc, cfg.get("api_key", ""))[:200])
            fails += 1
        sleep(min(cfg["poll"] * (2 ** min(fails, 4)), 600) if fails >= 3 else cfg["poll"])


# ------------------------------------------------------------------ skyrsla: baetir radgjofin?
def net_return(entry, exit_, fee, drag=0.0):
    return exit_ * (1 - drag) * (1 - fee) / (entry * (1 + drag) * (1 + fee)) - 1


def usdt(ret, fee, drag, stake):
    """Nettoavoxtun (midad vid kostnadarverd) -> USDT: stake * (1+skrid) * (1+gjald) * avoxtun."""
    return stake * (1 + drag) * (1 + fee) * ret


def _mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _diff(rows):
    a = [r for r, ok in rows if ok]
    v = [r for r, ok in rows if not ok]
    return _mean(a) - _mean(v) if a and v else None


def compare(rows, B=2000, seed=7):
    """rows: [(nettoavoxtun, samthykkt)]. Skilar medaltolum og bili fyrir (samthykkt - hafnad) + umrodunarprof."""
    rng = random.Random(seed)
    n = len(rows)
    obs = _diff(rows)
    boots = []
    for _ in range(B):
        d = _diff([rows[rng.randrange(n)] for _ in range(n)])
        if d is not None:
            boots.append(d)
    boots.sort()
    labels = [ok for _, ok in rows]
    rets = [r for r, _ in rows]
    ge = 0
    for _ in range(B):
        rng.shuffle(labels)
        d = _diff(list(zip(rets, labels)))
        if d is not None and obs is not None and d >= obs:
            ge += 1
    ci = (boots[int(0.05 * len(boots))], boots[min(len(boots) - 1, int(0.95 * len(boots)))]) if boots else None
    ap = [r for r, ok in rows if ok]
    vp = [r for r, ok in rows if not ok]
    return {"n": n, "n_approved": len(ap), "n_vetoed": len(vp), "mean_all": _mean(rets), "mean_approved": _mean(ap), "mean_vetoed": _mean(vp),
            "win_all": sum(1 for r in rets if r > 0) / n if n else None, "win_approved": (sum(1 for r in ap if r > 0) / len(ap)) if ap else None,
            "win_vetoed": (sum(1 for r in vp if r > 0) / len(vp)) if vp else None, "diff": obs, "ci90": ci, "p_perm": (ge + 1) / (B + 1)}


def verdict(c, min_total=200, min_group=30):
    if c["n"] < min_total or c["n_approved"] < min_group or c["n_vetoed"] < min_group:
        return "OF FA GOGN (%d kaup, %d samthykkt, %d hafnad; tharf >= %d og >= %d i hvorum hop)" % (c["n"], c["n_approved"], c["n_vetoed"], min_total, min_group)
    if c["ci90"] and c["ci90"][0] > 0 and c["p_perm"] <= 0.05:
        return "RADGJOFIN BAETIR (marktaekt: 90%% bil fyrir munin er yfir 0, p=%.3f)" % c["p_perm"]
    return "RADGJOFIN BAETIR EKKI (enn): munurinn er innan tilviljunar (p=%.3f)" % c["p_perm"]


def load_outcomes(cfg, state, now, drag=0.0, stale_min=30, prompt_version=None):
    """Utkoma hvers LIFANDI akvordunar: lokad -> raunverulegt soluverd; opid -> mat a sidasta kerti (adeins ef ferskt). Skilar (rows, stats)."""
    q = "SELECT trade_id, decision, decided_at, open_date, pair FROM shadow_decisions WHERE decision IN ('approve','veto')"
    decided = state.execute(q + (" AND prompt_version = ?" if prompt_version else ""), ((prompt_version,) if prompt_version else ())).fetchall()
    tcon, acon = ro_connect(cfg["trades_db"]), ro_connect(cfg["audit_db"])
    out, stats = [], {"closed": 0, "open": 0, "stale_open": 0, "not_live": 0}
    stats["other_version"] = (state.execute("SELECT COUNT(*) FROM shadow_decisions WHERE decision IN ('approve','veto') AND prompt_version IS NOT ?", (prompt_version,)).fetchone()[0]
                              if prompt_version else 0)
    try:
        for tid, dec, decided_at, open_date, pair in decided:
            if (parse_ft_date(decided_at) - parse_ft_date(open_date)).total_seconds() > 15 * 60:
                stats["not_live"] += 1
                continue
            row = tcon.execute("SELECT open_rate, close_rate, is_open FROM trades WHERE id = ?", (tid,)).fetchone()
            if row is None:
                continue
            entry, close_rate, is_open = row
            if not is_open and close_rate:
                exit_, kind = close_rate, "closed"
            else:
                c = acon.execute("SELECT date, payload FROM candles WHERE pair = ? ORDER BY date DESC LIMIT 1", (pair,)).fetchone()
                if c is None or (now - parse_ft_date(c[0])).total_seconds() > stale_min * 60:
                    stats["stale_open"] += 1
                    continue
                exit_, kind = float(json.loads(c[1])["close"]), "open"
            stats[kind] += 1
            out.append({"trade_id": tid, "approved": dec == "approve", "entry": entry, "exit": exit_, "kind": kind})
    finally:
        tcon.close()
        acon.close()
    return out, stats


def report(cfg, out=print, drag=0.0, min_total=200, now=None, B=2000, prompt_version=PROMPT_VERSION, stake=30.0):
    now = now or now_utc()
    state = open_state(cfg["state_db"])
    tot = state.execute("SELECT COUNT(*), SUM(decision IS NOT NULL), SUM(error IS NOT NULL), SUM(decision='approve'), SUM(decision='veto'), "
                        "SUM(error='no_features'), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), MIN(decided_at), MAX(decided_at) FROM shadow_decisions").fetchone()
    n_rows, n_dec, n_err, n_app, n_veto, n_nof, tin, tout, first, last = tot
    out("== Skuggastilling: radgjof Claude (%s, prompt %s) ==" % (cfg["model"], PROMPT_VERSION))
    if not n_rows:
        out("Engar akvardanir skradar enn. Thjonustan akvedur adeins um kaup sem opnast EFTIR ad hun fer i gang.")
        return None
    out("Akvardanir: %d (samthykkt %s, hafnad %s) | villur/an gagna: %s (an visa: %s) | fyrsta %s, sidasta %s" % (n_dec or 0, n_app or 0, n_veto or 0, n_err or 0, n_nof or 0, first, last))
    out("Token: %d inn, %d ut (til ad aetla kostnad i Console)" % (tin, tout))
    rows, st = load_outcomes(cfg, state, now, drag, prompt_version=prompt_version)
    out("Utkoma: %d lokud, %d opin (met a sidasta kerti), %d opin an ferskra gagna (sleppt), %d ekki-lifandi (sleppt)" % (st["closed"], st["open"], st["stale_open"], st["not_live"]))
    if st["other_version"]:
        out("ATH: %d akvardanir eru fra ANNARRI utgafu fyrirspurnar en %s og eru EKKI med (tilraunin ma ekki blandast)." % (st["other_version"], prompt_version))
    if n_dec and (n_app / n_dec > 0.95 or n_app / n_dec < 0.05):
        out("ATH: radgjofin samthykkir/hafnar nanast ALLT (%.0f%% samthykkt) - hun siar ekkert, svo samanburdur er litils virdi." % (100.0 * n_app / n_dec))
    results = {}
    for label, fee in FEES.items():
        data = [(net_return(r["entry"], r["exit"], fee, drag), r["approved"]) for r in rows]
        if not data:
            continue
        c = compare(data, B=B)
        c["total_all"] = sum(usdt(r, fee, drag, stake) for r, _ in data)
        c["total_approved"] = sum(usdt(r, fee, drag, stake) for r, ok in data if ok)
        results[label] = c
        ci = ("[%+.2f%% .. %+.2f%%]" % (100 * c["ci90"][0], 100 * c["ci90"][1])) if c["ci90"] else "-"
        pct = lambda x: ("%+.2f%%" % (100 * x)) if x is not None and not math.isnan(x) else "-"
        win = lambda x: ("%.0f%%" % (100 * x)) if x is not None else "-"
        out("\n-- Gjold: %s --" % label)
        out("  allt:       %4d kaup  medaltal %s  vinn %s" % (c["n"], pct(c["mean_all"]), win(c["win_all"])))
        out("  samthykkt:  %4d kaup  medaltal %s  vinn %s" % (c["n_approved"], pct(c["mean_approved"]), win(c["win_approved"])))
        out("  hafnad:     %4d kaup  medaltal %s  vinn %s" % (c["n_vetoed"], pct(c["mean_vetoed"]), win(c["win_vetoed"])))
        out("  munur (samthykkt - hafnad): %s  90%% bil %s  umrodunarprof p=%.3f" % (pct(c["diff"]), ci, c["p_perm"]))
        out("  heildarhagnadur (%d USDT/kaup): allt %+.2f | adeins samthykkt %+.2f | breyting %+.2f USDT (= thad sem hofnunin sparar/kostar)" % (
            stake, c["total_all"], c["total_approved"], c["total_approved"] - c["total_all"]))
        out("  NIDURSTADA: " + verdict(c, min_total=min_total))
    out("\nAthugasemd: allt er reiknad EFTIR gjold og adeins ur LIFANDI akvordunum. Botninn keypti ollum kaupunum; 'hafnad' er thvi thad sem hefdi sparast. "
        "Tengdu radgjofina vid raunverulegar akvardanir adeins ef NIDURSTADA er marktaek yfir nokkra daga og markadsstodur.")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--drag", type=float, default=0.0, help="valfrjalst skrid/spread a hlid i skyrslunni (t.d. 0.0015)")
    ap.add_argument("--min-total", type=int, default=200)
    ap.add_argument("--stake", type=float, default=30.0, help="USDT per kaup i heildarhagnadi skyrslunnar")
    ap.add_argument("--prompt-version", default=PROMPT_VERSION, help="adeins akvardanir fra thessari utgafu fyrirspurnar (sjalfgefid: nuverandi)")
    a = ap.parse_args(argv)
    cfg = load_config()
    if a.report:
        report(cfg, drag=a.drag, min_total=a.min_total, prompt_version=a.prompt_version, stake=a.stake)
        return 0
    if not cfg["api_key"]:
        log("ANTHROPIC_API_KEY vantar i umhverfinu (settu hann i ~/freqtrade/.env a netthjoninum - ALDREI i spjall). Haetti.")
        return 2
    if a.once:
        state = open_state(cfg["state_db"])
        print(decide_once(cfg, state, now_utc(), make_llm(cfg)))
        return 0
    run_forever(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
