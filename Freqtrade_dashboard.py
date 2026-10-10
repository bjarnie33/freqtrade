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
    FREQTRADE_API_URL=http://127.0.0.1:8081
    FREQTRADE_USERNAME=bjarni
    FREQTRADE_PASSWORD=lykilorðið-þitt-úr-config.json

Keyrðu: python3 Freqtrade_dashboard.py
Opnaðu: http://localhost:5050
"""

import os
import sys

import requests
from flask import Flask, jsonify, render_template_string

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)

FT_API_URL = os.environ.get("FREQTRADE_API_URL", "http://127.0.0.1:8081")
FT_USERNAME = os.environ.get("FREQTRADE_USERNAME")
FT_PASSWORD = os.environ.get("FREQTRADE_PASSWORD")

TESTNET_BASE_URL = "https://testnet.binance.vision"

AVAILABLE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT", "LINKUSDT",
]

if not FT_USERNAME or not FT_PASSWORD:
    print("Vantar FREQTRADE_USERNAME / FREQTRADE_PASSWORD í .env skjalinu.")
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
EXCHANGE_FEES = [
    {"key": "binance", "name": "Binance", "fee": float(os.environ.get("COMPARE_FEE_BINANCE", "0.001"))},
    {"key": "coinbase", "name": "Coinbase", "fee": float(os.environ.get("COMPARE_FEE_COINBASE", "0.005"))},
]


def _net_profit(amount, open_rate, close_rate, fee):
    """Hagnaður í USDT eftir gjöld á báðum hliðum, og kostnaðarverð (með kaupgjaldi)."""
    cost = amount * open_rate * (1 + fee)
    proceeds = amount * close_rate * (1 - fee)
    return proceeds - cost, cost


@app.route("/api/exchange-compare")
def api_exchange_compare():
    """Endurreiknar sömu viðskipti (sömu kaup- og söluverð) með gjöldum hverrar kauphallar.
    Þetta er EKKI ný hermun: merki og verð eru þau sömu, bara gjöldin breytast."""
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
                "profit_pct": t.get("profit_pct", 0),
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
        resp = requests.get(f"{TESTNET_BASE_URL}/api/v3/ticker/24hr",
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
        resp = requests.get(f"{TESTNET_BASE_URL}/api/v3/klines",
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
        resp = requests.get(f"{TESTNET_BASE_URL}/api/v3/depth",
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
<title>Freqtrade - Mælaborð</title>
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
    <div class="panel-title"><span>Binance vs. Coinbase &mdash; sömu viðskipti, ólík gjöld</span><span id="cmpNote" style="color:#8a8f98;font-weight:400;"></span></div>
    <table>
      <thead><tr><th></th><th id="cmpHeadBinance">Binance</th><th id="cmpHeadCoinbase">Coinbase</th><th>Munur</th></tr></thead>
      <tbody id="cmpBody"><tr><td colspan="4" style="color:#8a8f98;">Sæki gögn...</td></tr></tbody>
    </table>
    <div style="color:#8a8f98;font-size:11px;margin-top:6px;">Sömu kaup- og söluverð endurreiknuð með gjöldum hvorrar kauphallar á báðar hliðar. Ekki ný hermun: merki og verð eru óbreytt.</div>
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

async function loadExchangeCompare() {
  try {
    const res = await fetch('/api/exchange-compare');
    const d = await res.json();
    const body = document.getElementById('cmpBody');
    if (d.error) { body.innerHTML = '<tr><td colspan="4" style="color:#ef5350;">Villa: ' + d.error + '</td></tr>'; return; }
    const b = d.exchanges.find(e => e.key === 'binance');
    const c = d.exchanges.find(e => e.key === 'coinbase');
    document.getElementById('cmpHeadBinance').textContent = 'Binance (' + b.fee_pct.toFixed(2) + '%)';
    document.getElementById('cmpHeadCoinbase').textContent = 'Coinbase (' + c.fee_pct.toFixed(2) + '%)';
    if (d.booked_fee_pct !== null) {
      document.getElementById('cmpNote').textContent = 'Botninn skráði sjálfur: ' + d.booked_pnl.toFixed(2) + ' USDT (gjald ' + d.booked_fee_pct.toFixed(2) + '%)';
    }
    const money = (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + ' USDT';
    const cls = (v) => v >= 0 ? 'pnl-pos' : 'pnl-neg';
    const pct = (v) => v === null ? '-' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
    const wr = (v) => v === null ? '-' : v.toFixed(1) + '%';
    const rows = [
      ['Lokuð viðskipti', b.closed, c.closed, null],
      ['Unnin / töpuð', b.wins + ' / ' + b.losses, c.wins + ' / ' + c.losses, null],
      ['Sigurhlutfall', wr(b.winrate), wr(c.winrate), (b.winrate !== null) ? (c.winrate - b.winrate).toFixed(1) + ' pp' : '-'],
      ['Meðaltal á viðskipti', pct(b.avg_pct), pct(c.avg_pct), (b.avg_pct !== null) ? (c.avg_pct - b.avg_pct).toFixed(2) + ' pp' : '-'],
      ['Hagnaður/tap, lokuð', money(b.closed_pnl), money(c.closed_pnl), money(c.closed_pnl - b.closed_pnl)],
      ['Fljótandi, ' + b.open_count + ' opnar stöður', money(b.open_pnl), money(c.open_pnl), money(c.open_pnl - b.open_pnl)],
      ['Samtals', money(b.total_pnl), money(c.total_pnl), money(c.total_pnl - b.total_pnl)],
    ];
    const colored = new Set([4, 5, 6]);
    body.innerHTML = rows.map((r, i) => {
      const bc = colored.has(i) ? ' class="' + cls(i === 4 ? b.closed_pnl : i === 5 ? b.open_pnl : b.total_pnl) + '"' : '';
      const cc = colored.has(i) ? ' class="' + cls(i === 4 ? c.closed_pnl : i === 5 ? c.open_pnl : c.total_pnl) + '"' : '';
      const bold = i === 6 ? ' style="font-weight:700;"' : '';
      return '<tr' + bold + '><td>' + r[0] + '</td><td' + bc + '>' + r[1] + '</td><td' + cc + '>' + r[2] + '</td><td style="color:#8a8f98;">' + (r[3] === null ? '' : r[3]) + '</td></tr>';
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
      <td class="${cls}">${(t.profit_pct * 100).toFixed(2)}%</td></tr>`;
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
loadOpenTrades();
loadClosedTrades();

setInterval(loadTicker, 2000);
setInterval(loadDepth, 2000);
setInterval(loadSummary, 3000);
setInterval(loadPerformance, 10000);
setInterval(loadExchangeCompare, 10000);
setInterval(loadOpenTrades, 3000);
setInterval(loadClosedTrades, 8000);
setInterval(loadKlines, 30000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "5050"))
    print("Innskráning á Freqtrade API...")
    try:
        login()
        print("Innskráning tókst.")
    except Exception as exc:
        # Freqtrade er kannski ekki tilbúið (t.d. rétt eftir endurræsingu) - hver beiðni reynir aftur sjálf.
        print(f"Gat ekki skráð inn strax ({exc}) - reyni aftur sjálfkrafa.")
    print(f"Mælaborð keyrir á http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
