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

FT_API_URL = os.environ.get("FREQTRADE_PAPER_API_URL", "http://127.0.0.1:8082")
FT_USERNAME = os.environ.get("FREQTRADE_PAPER_USERNAME", "bjarni")
FT_PASSWORD = os.environ.get("FREQTRADE_PAPER_PASSWORD")

TESTNET_BASE_URL = "https://testnet.binance.vision"

AVAILABLE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT",
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
        data = ft_get("/api/v1/trades?limit=30")
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
  .summary-bar { display: flex; gap: 24px; padding: 10px 20px; background: #0d0f13; border-bottom: 1px solid #1e2128; }
</style>
</head>
<body>

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
      <table>
        <thead><tr><th>Mynt</th><th>Kaupverð → Núverandi</th><th>Hagnaður/Tap</th></tr></thead>
        <tbody id="openTradesBody"><tr><td colspan="3" style="color:#8a8f98;">Engar opnar stöður...</td></tr></tbody>
      </table>
    </div>
    <div class="bottom-panel">
      <div class="panel-title"><span>Lokuð viðskipti (Freqtrade)</span></div>
      <table>
        <thead><tr><th>Mynt</th><th>Ástæða</th><th>Verð</th><th>Hagnaður/Tap</th></tr></thead>
        <tbody id="closedTradesBody"><tr><td colspan="4" style="color:#8a8f98;">Engin lokuð viðskipti ennþá...</td></tr></tbody>
      </table>
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

async function loadTicker() {
  const res = await fetch(`/api/ticker/${currentSymbol}`);
  const d = await res.json();
  if (d.error) return;
  const priceEl = document.getElementById('lastPrice');
  const cls = d.price_change_pct >= 0 ? 'up' : 'down';
  const sign = d.price_change_pct >= 0 ? '+' : '';
  priceEl.innerHTML = `$${d.last_price.toLocaleString('en-US', {maximumFractionDigits: 6})} <span class="${cls}" style="font-size:12px">${sign}${d.price_change_pct.toFixed(2)}%</span>`;
  document.getElementById('statVolume').textContent = '$' + (d.volume_quote / 1e6).toFixed(2) + 'M';
  document.getElementById('statHigh').textContent = '$' + d.high.toLocaleString('en-US', {maximumFractionDigits: 6});
  document.getElementById('statLow').textContent = '$' + d.low.toLocaleString('en-US', {maximumFractionDigits: 6});
}

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
    return `<tr><td>${t.pair}</td><td>${t.open_rate?.toFixed(4)} → ${t.current_rate?.toFixed(4)}</td>
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
      <td>${t.open_rate?.toFixed(4)} → ${t.close_rate?.toFixed(4)}</td>
      <td class="${cls}">${t.profit_pct >= 0 ? '+' : ''}${t.profit_pct.toFixed(2)}%</td></tr>`;
  }).join('');
}

document.getElementById('symbolSelect').addEventListener('change', (e) => {
  currentSymbol = e.target.value;
  loadKlines();
  loadTicker();
  loadDepth();
});

initChart();
loadKlines();
loadTicker();
loadDepth();
loadSummary();
loadPerformance();
loadOpenTrades();
loadClosedTrades();

setInterval(loadTicker, 2000);
setInterval(loadDepth, 2000);
setInterval(loadSummary, 3000);
setInterval(loadPerformance, 10000);
setInterval(loadOpenTrades, 3000);
setInterval(loadClosedTrades, 8000);
setInterval(loadKlines, 30000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Innskráning á Freqtrade API...")
    login()
    print("Tekst! Mælaborð keyrir á http://localhost:5051")
    app.run(host="127.0.0.1", port=5051, debug=False)
