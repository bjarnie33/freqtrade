"""Causal candle features and offline, purged chronological model evaluation."""
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd

FEATURES = ['return_1', 'return_12', 'volume_ratio', 'volatility_12', 'sma_ratio', 'rsi_14', 'macd_ratio']

def features(frame):
    d = frame.copy()
    close = d['close']
    d['return_1'] = close.pct_change(fill_method=None)
    d['return_12'] = close.pct_change(12, fill_method=None)
    d['volume_ratio'] = d['volume'] / d['volume'].rolling(12).mean().replace(0, np.nan)
    d['volatility_12'] = d['return_1'].rolling(12).std()
    d['sma_ratio'] = close / close.rolling(24).mean() - 1
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    d['rsi_14'] = 100 - 100 / (1 + up / down.replace(0, np.nan))
    d.loc[(down == 0) & (up > 0), 'rsi_14'] = 100
    d.loc[(down == 0) & (up == 0), 'rsi_14'] = 50
    d['macd_ratio'] = (close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()) / close
    return d.replace([np.inf, -np.inf], np.nan)

def candles(path):
    d = pd.read_csv(path)
    d['date'] = pd.to_datetime(d['date'], utc=True)
    if not d['date'].is_monotonic_increasing or d['date'].duplicated().any():
        raise ValueError('Candles must be unique and chronological; one pair per CSV')
    if not {'open','high','low','close','volume'}.issubset(d):
        raise ValueError('Required: date,open,high,low,close,volume')
    v = d[['open','high','low','close','volume']]
    if not np.isfinite(v.to_numpy()).all() or (v[['open','high','low','close']] <= 0).any().any() or (v.volume < 0).any():
        raise ValueError('Invalid OHLCV')
    if (d.high < d[['open','close','low']].max(axis=1)).any() or (d.low > d[['open','close','high']].min(axis=1)).any():
        raise ValueError('Inconsistent OHLC prices')
    if len(d) > 1 and not d.date.diff().iloc[1:].eq(pd.Timedelta(minutes=5)).all():
        raise ValueError('Expected continuous closed 5-minute candles')
    if len(d) and d.date.iloc[-1] + pd.Timedelta(minutes=5) > pd.Timestamp.now(tz='UTC'):
        raise ValueError('Last candle is not closed')
    return d

def simulate(d, probabilities, fee=.001, spread_bps=10, slippage_bps=10, stake=50, cash=1700):
    """Signal at close t; fill at open t+1. One position, long-only, no leverage."""
    if not 0 <= fee < 1 or min(spread_bps, slippage_bps, stake, cash) < 0:
        raise ValueError('Invalid simulation parameters')
    drag = (spread_bps/2 + slippage_bps)/10000
    if drag >= 1:
        raise ValueError('Execution drag must be below 100%')
    qty = 0.; paid = 0.; trades = []; equity = []
    for i in range(1, len(d)):
        price = float(d.iloc[i].open); p = probabilities[i-1]
        if qty and p < .4:
            received = qty * price * (1-drag) * (1-fee)
            cash += received
            trades.append({'exit_time':str(d.iloc[i].date), 'profit': received-paid})
            qty = 0.
        elif not qty and p > .6:
            paid = min(stake, cash/(1+fee))
            if paid > 0:
                qty = paid/(price*(1+drag)); cash -= paid*(1+fee); paid *= 1+fee
        equity.append(cash + qty*float(d.iloc[i].close)*(1-drag)*(1-fee))
    return {'closed_trades':trades, 'open_quantity':qty, 'available_balance':cash,
            'final_liquidation_equity':equity[-1] if equity else cash,
            'assumptions':{'fee_per_side':fee,'spread_bps':spread_bps,'slippage_bps_per_side':slippage_bps,'stake':stake}}

def train(csv, output, horizon=12):
    import joblib
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, log_loss
    from sklearn.model_selection import TimeSeriesSplit
    if horizon < 1:
        raise ValueError('horizon must be positive')
    raw = candles(csv)
    d = features(raw)
    future = d.close.shift(-horizon)/d.close - 1
    d['label'] = (future > .005).astype(int)  # research target, not a trading guarantee
    d = d.loc[future.notna()].dropna(subset=FEATURES).reset_index(drop=True)
    if len(d) < max(300, horizon*20):
        raise ValueError('Not enough candles for purged chronological evaluation')
    cut = int(len(d)*.8)
    dev = d.iloc[:cut-horizon]; test = d.iloc[cut:]
    def fit(part):
        if part.label.nunique() != 2:
            raise ValueError('Training window needs both target classes')
        m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, random_state=42))
        return m.fit(part[FEATURES], part.label)
    folds = []
    for a,b in TimeSeriesSplit(n_splits=3, gap=horizon).split(dev):
        m = fit(dev.iloc[a]); p = m.predict_proba(dev.iloc[b][FEATURES])[:,1]
        folds.append({'train_end':str(dev.iloc[a[-1]].date), 'test_start':str(dev.iloc[b[0]].date),
                      'log_loss':float(log_loss(dev.iloc[b].label,p,labels=[0,1]))})
    model = fit(dev)
    p = model.predict_proba(test[FEATURES])[:,1]
    # All simulation candles are out of sample; no artificial jumps across invalid rows.
    simulation_frame = raw[(raw.date >= test.date.iloc[0]) & (raw.date <= test.date.iloc[-1])].reset_index(drop=True)
    if len(simulation_frame) != len(test):
        raise ValueError('Missing features inside test interval')
    report = {'features':FEATURES, 'horizon_candles':horizon, 'training_end':str(dev.date.iloc[-1]),
              'test_start':str(test.date.iloc[0]), 'test_end':str(test.date.iloc[-1]), 'folds':folds,
              'accuracy':float(accuracy_score(test.label,p>.5)),
              'log_loss':float(log_loss(test.label,p,labels=[0,1])), 'simulation':simulate(simulation_frame,p),
              'data_sha256':hashlib.sha256(Path(csv).read_bytes()).hexdigest()}
    version = hashlib.sha256(json.dumps(report,sort_keys=True).encode()).hexdigest()[:16]
    report['model_version'] = version
    out = Path(output)/version; out.mkdir(parents=True,exist_ok=False)
    joblib.dump({'model':model,'metadata':report},out/'model.joblib')
    (out/'report.json').write_text(json.dumps(report,indent=2))
    return out
