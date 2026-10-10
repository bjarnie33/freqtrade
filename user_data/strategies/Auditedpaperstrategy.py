"""Optional paper-only strategy. Uses RSI rules until an explicit research model is set."""
import json
import logging
import math
import sqlite3
import sys
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
import pandas as pd
from freqtrade.strategy import IStrategy
from freqtrade.persistence import Trade
sys.path.insert(0,str(Path(__file__).resolve().parent))
from paper_research import FEATURES, features
log=logging.getLogger(__name__)

class AuditedPaperStrategy(IStrategy):
    INTERFACE_VERSION=3
    timeframe='5m'
    startup_candle_count=100
    minimal_roi={'0':.10}
    stoploss=-.05
    can_short=False
    position_adjustment_enable=False

    def __init__(self,config):
        # Constructor is deliberately outside Freqtrade's callback exception fallback.
        if config.get('dry_run') is not True or config.get('trading_mode','spot') != 'spot':
            raise ValueError('AuditedPaperStrategy requires dry_run=true and spot')
        super().__init__(config)
        self.root=Path(config['user_data_dir'])
        # Áhættumörkin koma úr venjulegu Freqtrade-stillingunum (max_open_trades, stake_amount), svo ein tala ræður.
        # Sjálfgefin gildi (engar stillingar): 50 USDT á viðskipti, 3 opnar stöður, 50 USDT dagleg töp, 10 kaup á dag.
        def _num(v,default):
            try:
                v=float(v)
                return v if math.isfinite(v) and v>0 else default
            except (TypeError,ValueError):
                return default
        self.limit_stake=_num(config.get('stake_amount'),50.)             # 'unlimited' -> 50
        self.limit_open=int(_num(config.get('max_open_trades'),3))        # -1 / vantar -> 3
        self.limit_daily_loss=max(50.,.1*self.limit_stake*self.limit_open)  # 10% af mestu áhættu, aldrei undir 50
        self.limit_approvals=10 if self.limit_open<=3 else 4*self.limit_open  # 4 kaup á dag fyrir hverja stöðu
        self._last_reject={}   # pair -> (ástæða, kerti): sama höfnun er skráð einu sinni á kerti
        self._seen_candle={}   # pair -> síðasta kerti sem þegar er vistað (sparar gagnagrunnsskrif)
        self.audit_path=self.root/'paper_audit.sqlite'
        self.model=None
        self.model_version='rsi-research-v1'
        model_path=config.get('paper_model_path')
        if model_path:
            import joblib
            artifact=joblib.load(model_path)  # only load files produced locally by research.py
            self.model=artifact['model'];self.model_version=artifact['metadata']['model_version']
            if artifact['metadata']['features'] != FEATURES:
                raise ValueError('Model feature schema mismatch')
            self.model_end=pd.Timestamp(artifact['metadata']['test_end'])+pd.Timedelta(minutes=5)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, time TEXT, day TEXT, event TEXT, pair TEXT, accepted INTEGER, payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS candles (pair TEXT, date TEXT, payload TEXT, PRIMARY KEY(pair,date))')

    @contextmanager
    def _db(self):
        # sqlite3.Connection notað sem "with" sér bara um commit/rollback,
        # EKKI um að loka tengingunni sjálfri - á Windows veldur það því
        # að skráin situr læst (ekki hægt að eyða/flytja hana) löngu eftir
        # að þessu falli lýkur. Þessi útgáfa lokar tengingunni alltaf,
        # líka ef villa kemur upp.
        conn = sqlite3.connect(self.audit_path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _mode_ok(self):
        mode=getattr(self.config.get('runmode'),'value',self.config.get('runmode'))
        return self.config.get('dry_run') is True and mode == 'dry_run'

    def _write(self,event,pair,current_time,accepted,payload):
        with self._db() as db:
            db.execute('INSERT INTO decisions(time,day,event,pair,accepted,payload) VALUES(?,?,?,?,?,?)',
                       (current_time.isoformat(),current_time.astimezone(timezone.utc).date().isoformat(),event,pair,int(accepted),json.dumps(payload,allow_nan=False)))

    def populate_indicators(self,dataframe,metadata):
        d=features(dataframe)
        d['model_probability']=float('nan')
        if self.model is not None:
            valid=d[FEATURES].notna().all(axis=1)
            # Never use this research artifact in its own train/test interval.
            valid &= pd.to_datetime(d.date,utc=True) >= self.model_end
            if valid.any():
                d.loc[valid,'model_probability']=self.model.predict_proba(d.loc[valid,FEATURES])[:,1]
        return d

    def populate_entry_trend(self,dataframe,metadata):
        # RSI 30/70 í stað 50/50 - merkið undir 50 helst virkt í mjög
        # langan, samfelldan tíma (RSI er undir 50 u.þ.b. helming allra
        # tímapunkta), sem olli því að Freqtrade reyndi kaup endurtekið á
        # hverri lykkju þar til daglega samþykktarmarkið (10) kláraðist á
        # örfáum mínútum. Strangara viðmið gefur sjaldgæfari, markvissari
        # merki - sama lexía og við lærðum með aðalbotninn (SimpleRSIStrategy).
        condition=(dataframe.model_probability>.6) if self.model is not None else (dataframe.rsi_14<30)
        dataframe['enter_long']=0
        dataframe.loc[condition & (dataframe.volume>0),'enter_long']=1
        dataframe['enter_tag']=self.model_version
        return dataframe

    def populate_exit_trend(self,dataframe,metadata):
        condition=(dataframe.model_probability<.4) if self.model is not None else (dataframe.rsi_14>70)
        dataframe['exit_long']=0
        dataframe.loc[condition & (dataframe.volume>0),'exit_long']=1
        return dataframe

    def _snapshot(self,pair,current_time):
        d,_=self.dp.get_analyzed_dataframe(pair,self.timeframe)
        # Rétt eftir ræsingu (eða fyrir fyrstu greiningarlotu fyrir tiltekna
        # mynt) getur Freqtrade skilað alveg tómu gagnatafli, án 'date'
        # dálks yfirhöfuð. Athugum þetta FYRST - annars hrynur næsta lína
        # með AttributeError í stað skýrrar ValueError.
        if d is None or d.empty or "date" not in d.columns:
            raise ValueError("No analyzed dataframe available yet")
        # Freqtrade candle timestamps are candle OPEN times.
        eligible=d[pd.to_datetime(d.date,utc=True)+pd.Timedelta(minutes=5)<=pd.Timestamp(current_time)]
        if eligible.empty:
            raise ValueError('No closed candle available')
        row=eligible.iloc[-1]
        if pd.Timestamp(current_time)-(pd.Timestamp(row.date)+pd.Timedelta(minutes=5)) > pd.Timedelta(minutes=10):
            raise ValueError('Candle data is stale')
        payload={'model_version':self.model_version,'candle_open_time':str(row.date)}
        for key in ['open','high','low','close','volume']+FEATURES+['model_probability','enter_long','exit_long']:
            val=row.get(key)
            payload[key]=float(val) if val is not None and pd.notna(val) else None
        if any(payload[k] is None or not math.isfinite(payload[k]) for k in FEATURES):
            raise ValueError('Incomplete candle features')
        return payload

    def bot_loop_start(self,current_time,**kwargs):
        if not self._mode_ok():
            return
        try:
            for pair in self.dp.current_whitelist():
                try:
                    payload=self._snapshot(pair,current_time)
                    if self._seen_candle.get(pair)==payload['candle_open_time']:
                        continue
                    with self._db() as db:
                        db.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?)',(pair,payload['candle_open_time'],json.dumps(payload,allow_nan=False)))
                    self._seen_candle[pair]=payload['candle_open_time']
                except Exception:
                    log.exception('Paper candle audit failed for %s',pair)
        except Exception:
            log.exception('Paper audit loop failed')

    def custom_stake_amount(self,pair,current_time,current_rate,proposed_stake,min_stake,max_stake,leverage,entry_tag,side,**kwargs):
        if not self._mode_ok() or side!='long' or leverage!=1:
            return 0.
        stake=min(self.limit_stake,proposed_stake,max_stake)
        return stake if stake >= (min_stake or 0) else 0.

    def confirm_trade_entry(self,pair,order_type,amount,rate,time_in_force,current_time,entry_tag,side,**kwargs):
        # Catch everything here: Freqtrade otherwise defaults to accepting on callback error.
        try:
            if not self._mode_ok():
                return False
            day=current_time.astimezone(timezone.utc).date().isoformat()
            trades=Trade.get_trades_proxy()
            opened=[t for t in trades if t.is_open]
            todays=[t for t in trades if not t.is_open and t.close_date_utc and t.close_date_utc.date().isoformat()==day]
            # Gross realized daily losses, fees already included in close_profit_abs.
            losses=sum(min(0.,float(t.close_profit_abs or 0)) for t in todays)
            with self._db() as db:
                approvals=db.execute("SELECT COUNT(*) FROM decisions WHERE day=? AND event='entry' AND accepted=1",(day,)).fetchone()[0]
            payload=self._snapshot(pair,current_time)
            cost=amount*rate
            free=float(self.wallets.get_free(self.config['stake_currency']))
            reason='allowed'
            if not all(math.isfinite(x) and x>0 for x in [amount,rate,cost]): reason='invalid_size'
            elif side!='long': reason='long_only'
            elif (self.root/'EMERGENCY_STOP').exists(): reason='emergency_stop'
            elif cost>self.limit_stake+1e-6: reason='stake_limit'
            elif cost*(1+float(self.config.get('fee',.001)))>free: reason='balance_limit'
            elif len(opened)>=self.limit_open: reason='open_trade_limit'
            elif losses<=-self.limit_daily_loss: reason='daily_realized_loss_limit'
            elif approvals>=self.limit_approvals: reason='daily_entry_approval_limit'
            elif self.model is not None and payload['model_probability'] is None: reason='model_unavailable'
            payload.update(reason=reason,proposed_cost=cost,free_balance=free,daily_gross_realized_losses=losses,open_trades=len(opened),entry_approvals=approvals,order_type=order_type,rate=rate,amount=amount)
            accepted=reason=='allowed'
            if accepted:
                self._last_reject.pop(pair,None)
            else:
                # Með mörgum myntum hafnar Freqtrade sama kaupum í hverri lykkju (á 5 sek. fresti). Skráum hverja
                # höfnun (par + ástæða) einu sinni á kerti - annars fyllist gagnagrunnurinn (62 þús. raðir áður).
                key=(reason,payload['candle_open_time'])
                if self._last_reject.get(pair)==key:
                    return False
                self._last_reject[pair]=key
            self._write('entry',pair,current_time,accepted,payload)
            return accepted
        except Exception:
            log.exception('Paper entry blocked because audit/risk validation failed')
            return False

    def confirm_trade_exit(self,pair,trade,order_type,amount,rate,time_in_force,exit_reason,current_time,**kwargs):
        if not self._mode_ok():
            return False
        try:
            payload=self._snapshot(pair,current_time)
            payload.update(trade_id=trade.id,exit_reason=exit_reason,amount=amount,rate=rate)
            self._write('exit',pair,current_time,True,payload)
        except Exception:
            # Do not trap an existing paper position because audit storage is broken.
            log.exception('Paper exit audit failed')
        return True
