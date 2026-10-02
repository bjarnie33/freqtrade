import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'user_data'/'strategies'))
from paper_research import FEATURES,features,simulate,train

# Small adapter fakes isolate callback tests from missing exchange dependencies.
ft=types.ModuleType('freqtrade'); st=types.ModuleType('freqtrade.strategy'); pe=types.ModuleType('freqtrade.persistence')
class Base:
    def __init__(self,config): self.config=config
class Trade:
    rows=[]
    @classmethod
    def get_trades_proxy(cls): return cls.rows
st.IStrategy=Base; pe.Trade=Trade
_previous={k:sys.modules.get(k) for k in ['freqtrade','freqtrade.strategy','freqtrade.persistence']}
sys.modules.update({'freqtrade':ft,'freqtrade.strategy':st,'freqtrade.persistence':pe})
from AuditedPaperStrategy import AuditedPaperStrategy
for _key,_value in _previous.items():
    if _value is None: sys.modules.pop(_key,None)
    else: sys.modules[_key]=_value

def sample(n=1200):
    rng=np.random.default_rng(23); c=100*np.exp(np.cumsum(rng.normal(0,.009,n)))
    return pd.DataFrame({'date':pd.date_range('2025-01-01',periods=n,freq='5min',tz='UTC'), 'open':c,'high':c*1.01,'low':c*.99,'close':c,'volume':rng.uniform(1,10,n)})

class ResearchTests(unittest.TestCase):
    def test_features_do_not_change_when_future_added(self):
        d=sample(); pd.testing.assert_frame_equal(features(d.iloc[:200])[FEATURES],features(d).iloc[:200][FEATURES])
    def test_execution_next_open_and_costs(self):
        d=sample(3);d['open']=100.;d['close']=100.
        no_cost=simulate(d,[.9,.1,.5],fee=0,spread_bps=0,slippage_bps=0)
        cost=simulate(d,[.9,.1,.5])
        self.assertEqual(no_cost['final_liquidation_equity'],1700.)
        self.assertLess(cost['final_liquidation_equity'],1700.)
        self.assertAlmostEqual(cost['closed_trades'][0]['profit'],cost['final_liquidation_equity']-1700.)
        d.loc[0,'open']=9999.;self.assertEqual(simulate(d,[.9,.1,.5]),cost)
    def test_train_purges_and_versions(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'data.csv';sample().to_csv(path,index=False)
            result=train(path,Path(t)/'models')
            r=json.loads((result/'report.json').read_text())
            self.assertLess(pd.Timestamp(r['training_end'])+pd.Timedelta(minutes=60),pd.Timestamp(r['test_start']))
            for fold in r['folds']:
                self.assertLess(pd.Timestamp(fold['train_end'])+pd.Timedelta(minutes=60),pd.Timestamp(fold['test_start']))
            self.assertTrue((result/'model.joblib').exists())

class RiskTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);Trade.rows=[]
        self.now=datetime(2025,1,2,tzinfo=timezone.utc)
        self.config={'dry_run':True,'runmode':'dry_run','user_data_dir':self.root,'stake_currency':'USDT','fee':.001}
        self.bot=AuditedPaperStrategy(self.config)
        self.bot.wallets=types.SimpleNamespace(get_free=lambda _:1000.)
        d=features(sample(288));d['enter_long']=1;d['exit_long']=0;d['model_probability']=np.nan
        self.bot.dp=types.SimpleNamespace(get_analyzed_dataframe=lambda *_:(d,None),current_whitelist=lambda:['BTC/USDT'])
    def tearDown(self): self.temp.cleanup()
    def entry(self,amount=.5):
        return self.bot.confirm_trade_entry('BTC/USDT','limit',amount,100.,'GTC',self.now,None,'long')
    def test_constructor_blocks_live(self):
        with self.assertRaises(ValueError): AuditedPaperStrategy({**self.config,'dry_run':False})
    def test_entry_and_persistent_cap(self):
        self.assertTrue(self.entry())
        for _ in range(9): self.assertTrue(self.entry())
        self.bot=AuditedPaperStrategy(self.config)
        # same audit database after restart; reconnect adapters
        self.bot.wallets=types.SimpleNamespace(get_free=lambda _:1000.)
        d=features(sample(288));self.bot.dp=types.SimpleNamespace(get_analyzed_dataframe=lambda *_:(d,None))
        self.assertFalse(self.entry())
    def test_size_emergency_and_loss_limits(self):
        self.assertFalse(self.entry(.51))
        (self.root/'EMERGENCY_STOP').touch();self.assertFalse(self.entry())
        (self.root/'EMERGENCY_STOP').unlink()
        Trade.rows=[types.SimpleNamespace(is_open=False,close_date_utc=self.now,close_profit_abs=-50.)]
        self.assertFalse(self.entry())
    def test_balance_open_limit_and_audit_failure(self):
        self.bot.wallets=types.SimpleNamespace(get_free=lambda _:49.)
        self.assertFalse(self.entry())
        self.bot.wallets=types.SimpleNamespace(get_free=lambda _:1000.)
        Trade.rows=[types.SimpleNamespace(is_open=True) for _ in range(3)]
        self.assertFalse(self.entry());Trade.rows=[]
        self.bot._write=lambda *a: (_ for _ in ()).throw(OSError('disk unavailable'))
        self.assertFalse(self.entry())
    def test_incomplete_candle_excluded(self):
        d=features(sample(289)); d.loc[288,'date']=pd.Timestamp(self.now)
        self.bot.dp=types.SimpleNamespace(get_analyzed_dataframe=lambda *_:(d,None))
        self.assertEqual(self.bot._snapshot('BTC/USDT',self.now)['candle_open_time'],str(d.iloc[287].date))
    def test_emergency_does_not_block_paper_exit(self):
        (self.root/'EMERGENCY_STOP').touch()
        self.assertTrue(self.bot.confirm_trade_exit('BTC/USDT',types.SimpleNamespace(id=1),'limit',.5,100.,'GTC','stop_loss',self.now))

if __name__=='__main__': unittest.main()
