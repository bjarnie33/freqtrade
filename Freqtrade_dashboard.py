#!/usr/bin/env python3
"""
Sérsniðið mælaborð sem tengist BEINT við Freqtrade's eigið REST API og
teiknar lifandi línurit af heildarupphæð (total balance) sem hækkar og
lækkar í rauntíma eftir því sem opnar stöður breytast í verði.

HVERNIG ÞETTA VIRKAR
---------------------
1. Þessi vefþjónn (Flask) skráir sig inn á Freqtrade's API með notandanafni
   og lykilorði (sömu og þú notar til að skrá þig inn á FreqUI sjálft).
2. Hann sækir reglulega /api/v1/balance frá Freqtrade og les út heildar-
   upphæð í USDT (þessi tala breytist með markaðsverði á opnum stöðum).
3. Vafrasíðan sem þessi vefþjónn birtir sækir töluna á 2ja sek. fresti og
   teiknar hana á lifandi línurit.

ATHUGIÐ: Þetta er ALGJÖRLEGA AÐSKILIÐ frá Freqtrade sjálfu - keyrir sem
sjálfstætt forrit á þinni vél samhliða Docker-gáminum. Það breytir engu,
bara les gögn.

UPPSETNING
----------
pip install flask requests python-dotenv --break-system-packages

Í .env skjalinu þínu (sama möppu og þetta skjal), bættu við:
    FREQTRADE_API_URL=http://127.0.0.1:8081
    FREQTRADE_USERNAME=bjarni
    FREQTRADE_PASSWORD=lykilorðið-þitt-úr-config.json

Svo keyrðu:
    python3 freqtrade_dashboard.py

Opnaðu: http://localhost:5050
"""

import os
import sys
import time
import threading
from datetime import datetime, timezone

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

if not FT_USERNAME or not FT_PASSWORD:
    print("Vantar FREQTRADE_USERNAME / FREQTRADE_PASSWORD í .env skjalinu.")
    sys.exit(1)

# Geymir aðgangstáknið (token) í minni - endurnýjað sjálfkrafa ef það rennur út
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
    """Kallar á Freqtrade API, skráir inn aftur sjálfkrafa ef tákn er útrunnið."""
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


@app.route("/api/total")
def api_total():
    try:
        balance = ft_get("/api/v1/balance")
        total = balance.get("total", 0)
        return jsonify({
            "total": total,
            "currency": balance.get("stake", balance.get("symbol", "USDT")),
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
                "profit_abs": t.get("profit_abs", 0),
                "open_rate": t.get("open_rate"),
                "current_rate": t.get("current_rate"),
            }
            for t in status
        ]
        return jsonify(trades)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="is">
<head>
<meta charset="UTF-8">
<title>Freqtrade - Lifandi mælaborð</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root { color-scheme: dark; }
  body {
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    background: #0f1117; color: #e6e8ec; margin: 0; padding: 24px;
  }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #8b8f9a; font-size: 13px; margin-bottom: 20px; }
  .row { display: flex; gap: 20px; flex-wrap: wrap; }
  .card {
    background: #171a23; border: 1px solid #262a37; border-radius: 12px;
    padding: 16px; flex: 1; min-width: 320px;
  }
  .total-big { font-size: 40px; font-weight: 700; margin: 8px 0; }
  .total-big.up { color: #3ecf8e; }
  .total-big.down { color: #f6465d; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #262a37; }
  th { color: #8b8f9a; font-weight: 500; }
  .pnl-pos { color: #3ecf8e; font-weight: 600; }
  .pnl-neg { color: #f6465d; font-weight: 600; }
  canvas { max-height: 300px; }
</style>
</head>
<body>
  <h1>📊 Freqtrade - Heildarupphæð í rauntíma</h1>
  <div class="sub">Sótt beint úr Freqtrade API · uppfærist á 2ja sekúndna fresti</div>

  <div class="row">
    <div class="card" style="flex: 2;">
      <div class="total-big" id="totalDisplay">--</div>
      <canvas id="totalChart"></canvas>
    </div>

    <div class="card">
      <strong>Opnar stöður núna</strong>
      <table id="tradesTable">
        <thead>
          <tr><th>Mynt</th><th>Kaupverð → Núverandi</th><th>Hagnaður/Tap</th></tr>
        </thead>
        <tbody id="tradesBody">
          <tr><td colspan="3" style="color:#8b8f9a;">Engar opnar stöður núna...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

<script>
let lastTotal = null;
const MAX_POINTS = 100;

const ctx = document.getElementById('totalChart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      label: 'Heildarupphæð',
      data: [],
      borderColor: '#3ecf8e',
      backgroundColor: 'rgba(62,207,142,0.08)',
      tension: 0.25,
      pointRadius: 0,
      fill: true,
    }]
  },
  options: {
    responsive: true,
    animation: false,
    scales: {
      x: { ticks: { color: '#8b8f9a', maxTicksLimit: 8 }, grid: { color: '#1f2330' } },
      y: { ticks: { color: '#8b8f9a' }, grid: { color: '#1f2330' } },
    },
    plugins: { legend: { display: false } },
  }
});

async function fetchTotal() {
  try {
    const res = await fetch('/api/total');
    const data = await res.json();
    if (data.error) {
      document.getElementById('totalDisplay').textContent = 'Villa: ' + data.error;
      return;
    }

    const el = document.getElementById('totalDisplay');
    el.textContent = data.total.toLocaleString('is-IS', {maximumFractionDigits: 2}) + ' ' + data.currency;
    if (lastTotal !== null) {
      el.className = 'total-big ' + (data.total >= lastTotal ? 'up' : 'down');
    }
    lastTotal = data.total;

    const label = new Date(data.timestamp).toLocaleTimeString('is-IS');
    chart.data.labels.push(label);
    chart.data.datasets[0].data.push(data.total);
    if (chart.data.labels.length > MAX_POINTS) {
      chart.data.labels.shift();
      chart.data.datasets[0].data.shift();
    }
    chart.update();
  } catch (e) {
    console.error(e);
  }
}

async function fetchOpenTrades() {
  try {
    const res = await fetch('/api/open-trades');
    const trades = await res.json();
    const body = document.getElementById('tradesBody');

    if (!Array.isArray(trades) || trades.length === 0) {
      body.innerHTML = '<tr><td colspan="3" style="color:#8b8f9a;">Engar opnar stöður núna...</td></tr>';
      return;
    }

    body.innerHTML = trades.map(t => {
      const pnlClass = t.profit_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
      return `<tr>
        <td>${t.pair}</td>
        <td>${t.open_rate?.toFixed(4)} → ${t.current_rate?.toFixed(4)}</td>
        <td class="${pnlClass}">${(t.profit_pct * 100).toFixed(2)}%</td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.error(e);
  }
}

fetchTotal();
fetchOpenTrades();
setInterval(fetchTotal, 2000);
setInterval(fetchOpenTrades, 3000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("Innskráning á Freqtrade API...")
    login()
    print(f"Tekst! Mælaborð keyrir á http://localhost:5050")
    app.run(host="127.0.0.1", port=5050, debug=False)