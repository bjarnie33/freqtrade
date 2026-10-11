#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fee_research.py - gjaldamedvituð rannsókn á kertum úr paper_audit.sqlite (pandas + numpy, ekkert sklearn).

Spurningar sem hún svarar, EFTIR gjöld (0,5% Coinbase, 0,1% Binance) + spread/verðskrið:
  1. Hvað skila RSI<30 kaupin í raun, og hve stór hluti þeirra nær yfir kostnaðinn?
  2. Hvaða útgangsreglur henta betur (RSI>70, RSI>70 aðeins ef hagnaður dekkar gjöld, RSI>50, take-profit)?
  3. Hvaða aðstæður (RSI, magn, sveifla, markaðurinn) skila betri kaupum?
  4. Getur einfalt líkan (ridge) valið betri kaup - mælt á tímabili sem það sá ALDREI (walk-forward)?

  python fee_research.py --db /work/user_data/paper_audit.sqlite [--days 14] [--stop 0.05]

Hermun: merki á lokun kertis t, kaup á OPNUN t+1, sala á opnun kertis eftir merki. Stop-loss við -5%
(sama og botninn). Viðskipti sem eru ekki búin þegar gögnin enda eru metin á lokaverði (ekki hent - annars lítur
niðurstaðan betur út en hún er, því töpin sitja einmitt í opnum stöðum).
"""
import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

FEATURES = ['return_1', 'return_12', 'volume_ratio', 'volatility_12', 'sma_ratio', 'rsi_14', 'macd_ratio']
MODEL_FEATURES = FEATURES + ['btc_ret12']
FEES = {'Binance 0.1%': 0.001, 'Coinbase maker 0.25%': 0.0025, 'Coinbase 0.5%': 0.005}   # Intro-threp: maker 0,25% / taker 0,50%
DRAG = 0.0015            # (spread 10 bps / 2 + slippage 10 bps) per hlid - sama og simulate() i paper_research.py
MAX_HOLD = 288           # 24 klst. (kertum)
STEP = pd.Timedelta(minutes=5)


def net_return(entry, exit_, fee, drag=DRAG):
    """Nettoavoxtun eftir gjold og verdskrid a BAÐAR hliðar."""
    return (exit_ * (1 - drag) * (1 - fee)) / (entry * (1 + drag) * (1 + fee)) - 1


@dataclass(frozen=True)
class Policy:
    name: str
    rsi_exit: float = None       # selja thegar RSI > thetta (a lokun kertis)
    min_gain: float = None       # ...en adeins ef brutto hagnadur >= thetta
    tp: float = None             # take-profit (limit) vid +x%


POLICIES = [
    Policy('RSI>70 (nuverandi)', rsi_exit=70),
    Policy('RSI>70 og hagnadur>=1.3%', rsi_exit=70, min_gain=0.013),
    Policy('RSI>50', rsi_exit=50),
    Policy('take-profit +2%', tp=0.02),
    Policy('take-profit +3%', tp=0.03),
]


# ------------------------------------------------------------------ gogn
def load_candles(db_path, days=None):
    con = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True, timeout=30)
    rows = []
    for pair, date, payload in con.execute('SELECT pair, date, payload FROM candles'):
        p = json.loads(payload)
        try:
            rows.append((pair, date, float(p['open']), float(p['high']), float(p['low']), float(p['close']), float(p['volume']),
                         *[float(p[f]) for f in FEATURES]))
        except (KeyError, TypeError, ValueError):
            continue
    con.close()
    d = pd.DataFrame(rows, columns=['pair', 'date', 'open', 'high', 'low', 'close', 'volume'] + FEATURES)
    d['date'] = pd.to_datetime(d['date'], utc=True)
    d = d.drop_duplicates(['pair', 'date']).sort_values(['pair', 'date']).reset_index(drop=True)
    if days:
        d = d[d['date'] >= d['date'].max() - pd.Timedelta(days=days)].reset_index(drop=True)
    return d


def segments(d):
    """Samfelldir bútar (5 mín. bil) - eyður (botninn var stopp) rjufa bútinn, hermun fer aldrei yfir eyðu."""
    for pair, g in d.groupby('pair', sort=False):
        brk = (g['date'].diff() != STEP).to_numpy()
        seg_id = np.cumsum(brk)
        for _, s in g.groupby(seg_id, sort=False):
            if len(s) >= 40:
                yield pair, s.reset_index(drop=True)


# ------------------------------------------------------------------ hermun
def run_segment(s, pol, stop=0.05, rsi_entry=30.0):
    o, h, l, c = (s[k].to_numpy() for k in ('open', 'high', 'low', 'close'))
    rsi = s['rsi_14'].to_numpy()
    t = s['date'].to_numpy()
    n = len(s)
    out = []
    i = 0
    while i < n - 1:
        if not rsi[i] < rsi_entry:
            i += 1
            continue
        e = i + 1
        entry = o[e]
        exit_i = exit_price = reason = None
        j = e
        while j < n:
            if l[j] <= entry * (1 - stop):
                exit_i, exit_price, reason = j, entry * (1 - stop), 'stop'
                break
            if pol.tp is not None and h[j] >= entry * (1 + pol.tp):
                exit_i, exit_price, reason = j, entry * (1 + pol.tp), 'tp'
                break
            if pol.rsi_exit is not None and rsi[j] > pol.rsi_exit and (pol.min_gain is None or c[j] / entry - 1 >= pol.min_gain):
                if j + 1 < n:
                    exit_i, exit_price, reason = j + 1, o[j + 1], 'signal'
                break
            if j - e >= MAX_HOLD:
                if j + 1 < n:
                    exit_i, exit_price, reason = j + 1, o[j + 1], 'timeout'
                break
            j += 1
        feats = {f: s[f].iloc[i] for f in FEATURES}
        if exit_price is None:     # oleyst thegar gognin enda: metid a lokaverdi
            out.append(dict(entry_time=t[e], exit_time=t[n - 1], entry=entry, exit=c[n - 1], reason='open', resolved=False,
                            signal_time=t[i], **feats))
            break
        out.append(dict(entry_time=t[e], exit_time=t[exit_i], entry=entry, exit=exit_price, reason=reason, resolved=True,
                        signal_time=t[i], **feats))
        i = exit_i
    return out


def build_events(d, pol, stop=0.05):
    ev = []
    for pair, s in segments(d):
        for e in run_segment(s, pol, stop):
            e['pair'] = pair
            ev.append(e)
    ev = pd.DataFrame(ev)
    if ev.empty:
        return ev
    # Botninn heldur stöðu áfram yfir eyðu (stopp/endurræsing) og pör detta út af listanum. Þá vitum við EKKI útkomuna.
    # Slík kaup eru EKKI seld með fullum kostnaði (það hallaði niðurstöðunni neikvætt) - þau eru tekin út og talin sér.
    tmax = d['date'].max()
    cens = (~ev['resolved']) & (pd.to_datetime(ev['exit_time'], utc=True) < tmax - pd.Timedelta(minutes=30))
    n_cens = int(cens.sum())
    ev = ev[~cens].copy()
    if ev.empty:
        ev.attrs['censored'] = n_cens
        return ev
    ev['gross'] = ev['exit'] / ev['entry'] - 1
    for name, fee in FEES.items():
        ev['net ' + name] = [net_return(a, b, fee) for a, b in zip(ev['entry'], ev['exit'])]
    btc = d[d['pair'] == 'BTC/USDT'].set_index('date')['return_12']
    ev['btc_ret12'] = pd.to_datetime(ev['signal_time'], utc=True).map(btc).fillna(0.0).to_numpy()
    ev = ev.sort_values('entry_time').reset_index(drop=True)
    ev.attrs['censored'] = n_cens
    return ev


# ------------------------------------------------------------------ likan (ridge, walk-forward)
def ridge_fit(X, y, lam=10.0):
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Z = np.clip((X - mu) / sd, -5, 5)
    w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
    return {'mu': mu, 'sd': sd, 'w': w, 'b': float(y.mean())}


def ridge_predict(m, X):
    return np.clip((X - m['mu']) / m['sd'], -5, 5) @ m['w'] + m['b']


def block_ci(times, values, block='6h', draws=2000, seed=7):
    """90% bil fyrir medaltal; endurtaka heila 6 klst. kubba (kaup lenda saman i thorrum markadarins)."""
    df = pd.DataFrame({'b': pd.DatetimeIndex(pd.to_datetime(times, utc=True)).floor(block), 'v': np.asarray(values)})
    groups = [g['v'].to_numpy() for _, g in df.groupby('b')]
    if len(groups) < 3:
        return float('nan'), float('nan')
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(draws):
        pick = rng.integers(0, len(groups), len(groups))
        vals = np.concatenate([groups[k] for k in pick])
        means.append(vals.mean())
    return float(np.percentile(means, 5)), float(np.percentile(means, 95))


def block_diff_ci(times, values, mask, block='6h', draws=2000, seed=11):
    """90% bil fyrir MUN (medaltal valinna kaupa - medaltal allra kaupa), sömu kubbar endurteknir fyrir bæði."""
    df = pd.DataFrame({'b': pd.DatetimeIndex(pd.to_datetime(times, utc=True)).floor(block), 'v': np.asarray(values), 'm': np.asarray(mask, bool)})
    groups = [(g['v'].to_numpy(), g['m'].to_numpy()) for _, g in df.groupby('b')]
    if len(groups) < 8:
        return float('nan'), float('nan')
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(draws):
        pick = rng.integers(0, len(groups), len(groups))
        v = np.concatenate([groups[k][0] for k in pick]); m = np.concatenate([groups[k][1] for k in pick])
        if m.sum() == 0:
            continue
        diffs.append(v[m].mean() - v.mean())
    if not diffs:
        return float('nan'), float('nan')
    return float(np.percentile(diffs, 5)), float(np.percentile(diffs, 95))


def walk_forward(ev, fee_col, train_frac=0.6, min_events=200):
    ev = ev.dropna(subset=MODEL_FEATURES + [fee_col]).sort_values('entry_time').reset_index(drop=True)
    if len(ev) < min_events:
        return {'error': 'of fa kaup (%d, tharf >= %d)' % (len(ev), min_events)}
    cut = int(len(ev) * train_frac)
    test_start = ev['entry_time'].iloc[cut]
    train = ev[(ev['entry_time'] < test_start) & (ev['exit_time'] < test_start)]     # hreinsun: ekkert sem skarast prófunartímann
    test = ev[ev['entry_time'] >= test_start].reset_index(drop=True)
    if len(train) < 100 or len(test) < 50:
        return {'error': 'of fa kaup eftir hreinsun (thjalfun %d, prof %d)' % (len(train), len(test))}
    y = np.clip(train[fee_col].to_numpy(), -0.06, 0.06)
    m = ridge_fit(train[MODEL_FEATURES].to_numpy(), y)
    pred = ridge_predict(m, test[MODEL_FEATURES].to_numpy())
    res = {'train_n': len(train), 'test_n': len(test), 'test_start': str(test_start), 'sets': []}
    sets = [('allt (grunnlina)', np.ones(len(test), bool)),
            ('efsti 20% spar', pred >= np.quantile(pred, 0.8)),
            ('spar > 0%', pred > 0.0),
            ('spar > +0.3%', pred > 0.003)]
    for name, mask in sets:
        sel = test.loc[mask, fee_col].to_numpy()
        if len(sel) == 0:
            res['sets'].append({'name': name, 'n': 0})
            continue
        lo, hi = block_ci(test.loc[mask, 'entry_time'], sel)
        entry = {'name': name, 'n': int(mask.sum()), 'mean': float(sel.mean()), 'win': float((sel > 0).mean()), 'lo': lo, 'hi': hi}
        if name != sets[0][0]:
            entry['diff_lo'], entry['diff_hi'] = block_diff_ci(test['entry_time'], test[fee_col], mask)
        res['sets'].append(entry)
    res['weights'] = {f: float(w) for f, w in zip(MODEL_FEATURES, m['w'])}
    return res


# ------------------------------------------------------------------ skýrsla
def pct(x):
    return '%+.2f%%' % (x * 100)


def tier_section(base, d, out, tiers_path, stake):
    """Gjaldathrep (yfirlag): botninn byrjar i lagsta threpi og velta safnast. Reiknad ofan a RSI>70-kaupin (30 USDT per kaup)."""
    try:
        import tier_engine as te
    except ImportError:
        out('\n== 5. Gjaldathrep == sleppt (tier_engine.py vantar i somu moppu og fee_research.py)')
        return None
    try:
        tables, warns = te.load_tiers(tiers_path)
    except ValueError as exc:
        out('\n== 5. Gjaldathrep == VILLA: %s' % exc)
        return None
    epoch = pd.Timestamp('1970-01-01', tz='UTC')
    ms = lambda x: int((pd.Timestamp(x, tz='UTC') - epoch) // pd.Timedelta(milliseconds=1)) if pd.Timestamp(x).tzinfo is None else int((pd.Timestamp(x) - epoch) // pd.Timedelta(milliseconds=1))
    trades = []
    for r in base.itertuples():
        trades.append({'open_ts': ms(r.entry_time), 'close_ts': ms(r.exit_time) if r.resolved else None, 'amount': stake / r.entry,
                       'open_rate': r.entry, 'close_rate': r.exit if r.resolved else None, 'current_rate': None if r.resolved else r.exit})
    now_ms = ms(d['date'].max())
    out('\n== 5. Gjaldathrep (hermt yfirlag): RSI>70-kaupin, %d USDT per kaup, botninn byrjar i lagsta threpi ==' % stake)
    for w in warns:
        out('  ! ' + w)
    results = {}
    for ex in ('coinbase', 'binance'):
        res = te.analyze(trades, tables[ex], now_ms, modes=('taker', 'maker'), drag=DRAG)
        results[ex] = res
        tk = res['modes']['taker']['tiered']
        out('%s: threp nu %s | velta 30 d. $%s | hradi $%s/dag' % (tables[ex]['label'], tk['tier'], format(round(tk['volume_30d']), ','), format(round(tk['pace_per_day']), ',')))
        if res['missing']:
            out('   ! throskuldar vantar fyrir %d threp (%s...) - fylltu inn tiers.json; thar til helst hermunin i %s' % (
                len(res['missing']), ', '.join(res['missing'][:3]), tables[ex]['tiers'][0]['name']))
        nx = tk['next']
        if nx and nx['min_volume'] is not None:
            eta = ('eftir um %.1f daga' % nx['eta_days']) if nx['eta_days'] is not None else 'naest EKKI a thessum hrada'
            out('   naesta threp: %s vid $%s (vantar $%s) - %s' % (nx['name'], format(round(nx['min_volume']), ','), format(round(nx['remaining']), ','), eta))
        for m in ('taker', 'maker'):
            r = res['modes'][m]
            out('   %-5s: med threpum %+9.2f USDT | fast %s %+9.2f USDT | sparnadur %+8.2f USDT | gjold greidd %.2f USDT' % (
                m, r['tiered']['total'], tables[ex]['tiers'][0]['name'], r['fixed']['total'], r['savings'], r['tiered']['fees_paid']))
        for e in tk['timeline'][-5:]:
            out('   threpabreyting: %s %s -> %s (velta $%s)' % (pd.Timestamp(e['ts'], unit='ms', tz='UTC').strftime('%m-%d %H:%M'), e['from'], e['tier'], format(round(e['volume']), ',')))
    out('  Athugasemd: "sparnadur" er hagnadurinn af lægri gjoldum. Ef "fast" talan er neikvaed borgar botninn med tapi fyrir veltuna - '
        'threpin laga thad adeins ef sparnadurinn naer yfir tapid.')
    return results


def report(d, stop=0.05, out=print, tiers_path=None, stake=30.0):
    days = (d['date'].max() - d['date'].min()).total_seconds() / 86400
    out('Gogn: %d kerti, %d por, %s -> %s (%.1f dagar)' % (len(d), d['pair'].nunique(), d['date'].min().strftime('%Y-%m-%d %H:%M'),
                                                      d['date'].max().strftime('%Y-%m-%d %H:%M'), days))
    if days < 14:
        out('!! Adeins %.1f dagar af gognum - EIN markadsstada. Taktu thessu sem fyrstu mynd, ekki sonnun (bidja 2-4 vikur).' % days)
    cb, bn, mk = 'net Coinbase 0.5%', 'net Binance 0.1%', 'net Coinbase maker 0.25%'
    base = None
    out('\n== 1+2. RSI<30 kaup og utgangsreglur (stop-loss -%d%%, kostnadur: gjald + 0.15%% skrid a hlid) ==' % round(stop * 100))
    out('%-28s %6s %6s | %-18s | %-18s | %-18s' % ('Utgangsregla', 'kaup', 'buin', 'Binance 0.1%', 'Coinbase maker 0.25%', 'Coinbase taker 0.5%'))
    out('%-28s %6s %6s | %-18s | %-18s | %-18s' % ('', '', '', 'medaltal / vinn', 'medaltal / vinn', 'medaltal / vinn'))
    all_events = {}
    for pol in POLICIES:
        ev = build_events(d, pol, stop)
        all_events[pol.name] = ev
        if ev.empty:
            out('%-28s engin kaup' % pol.name)
            continue
        censored = ev.attrs.get('censored', 0)
        out('%-28s %6d %5.0f%% | %+7.2f%% / %4.0f%% | %+7.2f%% / %4.0f%% | %+7.2f%% / %4.0f%%' % (
            pol.name, len(ev), ev['resolved'].mean() * 100, ev[bn].mean() * 100, (ev[bn] > 0).mean() * 100,
            ev[mk].mean() * 100, (ev[mk] > 0).mean() * 100, ev[cb].mean() * 100, (ev[cb] > 0).mean() * 100))
        if base is None:
            base = ev
    if base is None or base.empty:
        out('Engin RSI<30 kaup i gognunum.')
        return {}
    res = base[base['resolved']]
    out('\nAthugasemd: "buin" er hlutfall kaupa sem eru lokud. Oleyst kaup VID LOK GAGNANNA eru metin a lokaverdi (ekki hent).')
    out('  Utilokad (RSI>70): %d kaup sem klofnudu vid eydu i gognum eda thegar par for ur listanum - utkoman er othekkt, their eru ekki seld med kostnadi.'
        % base.attrs.get('censored', 0))
    out('  RSI>70, Coinbase: ÖLL kaup %+.2f%% (n=%d) en adeins LOKUD kaup %+.2f%% (n=%d) - munurinn eru toepin sem sitja enn i opnum stodum.'
        % (base[cb].mean() * 100, len(base), res[cb].mean() * 100, len(res)))
    for label, thr in (('Binance 0.1%', 0.005), ('Coinbase maker 0.25%', 0.008), ('Coinbase taker 0.5%', 0.013)):
        out('  Brutto haekkun >= %.1f%% (dekkar kostnad %s): %.0f%% allra kaupa' % (thr * 100, label, (base['gross'] >= thr).mean() * 100))

    out('\n== 2b. Dag fyrir dag (RSI>70-reglan): breytist niðurstaðan eftir markaðnum? ==')
    day = pd.to_datetime(base['entry_time'], utc=True).dt.strftime('%m-%d')
    out('  %-7s %5s %7s %10s %10s %10s %9s' % ('dagur', 'kaup', 'buin', 'Binance', 'CB maker', 'CB taker', 'vinn(CB)'))
    for dname, g in base.groupby(day):
        out('  %-7s %5d %6.0f%% %+9.2f%% %+9.2f%% %+9.2f%% %8.0f%%' % (dname, len(g), g['resolved'].mean() * 100, g[bn].mean() * 100, g[mk].mean() * 100, g[cb].mean() * 100, (g[cb] > 0).mean() * 100))

    out('\n== 3. Adstaedur (RSI>70-reglan, Coinbase 0.5%): fjordungar eftir hverjum visi (nettoavoxtun) ==')
    for f in ['rsi_14', 'volume_ratio', 'return_12', 'volatility_12', 'btc_ret12']:
        vals = base[f].to_numpy()
        edges = np.unique(np.quantile(vals, [0, .25, .5, .75, 1]))
        if len(edges) < 3:
            continue
        idx = np.clip(np.searchsorted(edges, vals, side='right') - 1, 0, len(edges) - 2)
        cells = []
        for k in range(len(edges) - 1):
            m = idx == k
            if m.any():
                cells.append('%.3g..%.3g: n=%d %+.2f%%' % (edges[k], edges[k + 1], m.sum(), base[cb].to_numpy()[m].mean() * 100))
        out('  %-14s ' % f + ' | '.join(cells))

    out('\n== 4. Likan (ridge a nettoavoxtun, Coinbase) - walk-forward: thjalfad a fyrri hluta, metid a seinni hluta sem thad sa ALDREI ==')
    wf = walk_forward(base, cb)
    base_verdict = verdict = None
    if 'error' in wf:
        out('  Sleppt: ' + wf['error'])
    else:
        out('  Thjalfun: %d kaup fyrir %s, prof: %d kaup eftir thad' % (wf['train_n'], wf['test_start'][:16], wf['test_n']))
        out('  %-18s %6s %9s %6s  %-24s %s' % ('Val', 'kaup', 'medaltal', 'vinn', '90% bil', 'munur vs allt (90% bil)'))
        for st in wf['sets']:
            if st['n'] == 0:
                out('  %-18s %6d' % (st['name'], 0))
                continue
            if 'diff_lo' in st and st['diff_lo'] == st['diff_lo'] and st['n'] >= 30:
                diff = '[%+.2f%% .. %+.2f%%]' % (st['diff_lo'] * 100, st['diff_hi'] * 100)
            else:
                diff = '(of fa kaup, <30)' if 'diff_lo' in st else ''
            ci = ('[%+.2f%% .. %+.2f%%]' % (st['lo'] * 100, st['hi'] * 100)) if st['lo'] == st['lo'] else '(of fa kaup)'
            out('  %-18s %6d %+8.2f%% %5.0f%%  %-20s %s' % (st['name'], st['n'], st['mean'] * 100, st['win'] * 100, ci, diff))
        b0 = wf['sets'][0]
        if b0['lo'] > 0:
            base_verdict = 'RSI-reglan ein og ser: JAKVAED eftir gjold (90%% bil [%+.2f%% .. %+.2f%%])' % (b0['lo'] * 100, b0['hi'] * 100)
        elif b0['hi'] < 0:
            base_verdict = 'RSI-reglan ein og ser: TAPAR eftir gjold (90%% bil [%+.2f%% .. %+.2f%%])' % (b0['lo'] * 100, b0['hi'] * 100)
        else:
            base_verdict = 'RSI-reglan ein og ser: OVIST (90%% bil [%+.2f%% .. %+.2f%%] nær yfir 0)' % (b0['lo'] * 100, b0['hi'] * 100)
        good = [st for st in wf['sets'][1:] if st['n'] >= 30 and st.get('diff_lo', -1) == st.get('diff_lo', -1) and st.get('diff_lo', -1) > 0]
        if good:
            g = max(good, key=lambda st: st['diff_lo'])
            verdict = 'LIKAN BAETIR: "%s" er %+.2f%% betra en medaltal profunartimans per kaup (90%% bil a muninum [%+.2f%% .. %+.2f%%]) - en adeins a %.1f dogum. Endurtaka med meiri gognum.' % (
                g['name'], (g['mean'] - b0['mean']) * 100, g['diff_lo'] * 100, g['diff_hi'] * 100, days)
        else:
            verdict = 'LIKAN BAETIR EKKI (enn): ekkert val er marktaekt betra en medaltal profunartimans.'
        out('\nNidurstada 1: ' + base_verdict)
        out('Nidurstada 2: ' + verdict)
        if days < 14:
            out('(Adeins %.1f dagar af gognum: tekid sem fyrsta mynd.)' % days)
    tiers = tier_section(base, d, out, tiers_path, stake)
    return {'wf': wf, 'verdict': verdict, 'base_verdict': base_verdict, 'tiers': tiers}


def main(argv=None):
    ap = argparse.ArgumentParser(description='Gjaldamedvituð rannsókn á paper_audit.sqlite')
    ap.add_argument('--db', required=True)
    ap.add_argument('--days', type=int, default=None, help='aðeins síðustu N dagar')
    ap.add_argument('--stop', type=float, default=0.05)
    ap.add_argument('--tiers', default=None, help='tiers.json (throskuldar). Sjalfgefid: tiers.json i sömu moppu og gagnagrunnurinn, ef hun er til')
    ap.add_argument('--stake', type=float, default=30.0, help='USDT per kaup i threpahermi')
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    d = load_candles(a.db, a.days)
    if d.empty:
        print('Engin kerti i gagnagrunninum.')
        return 1
    tiers_path = a.tiers
    if tiers_path is None:
        guess = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(a.db))), 'tiers.json')     # ~/freqtrade/tiers.json
        tiers_path = guess if os.path.isfile(guess) else None
    report(d, a.stop, tiers_path=tiers_path, stake=a.stake)
    return 0


if __name__ == '__main__':
    sys.exit(main())
