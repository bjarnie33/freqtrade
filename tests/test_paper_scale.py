import sqlite3, tempfile, types, unittest
from datetime import datetime, timezone
from pathlib import Path
from tests.test_paper import AuditedPaperStrategy, Trade, sample, features, np

class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name); Trade.rows = []
        self.now = datetime(2025, 1, 2, tzinfo=timezone.utc)
    def tearDown(self): self.temp.cleanup()
    def bot(self, **extra):
        cfg = {'dry_run': True, 'runmode': 'dry_run', 'user_data_dir': self.root, 'stake_currency': 'USDT', 'fee': .001, **extra}
        b = AuditedPaperStrategy(cfg); b.wallets = types.SimpleNamespace(get_free=lambda _: 100000.)
        d = features(sample(288)); d['enter_long'] = 1; d['exit_long'] = 0; d['model_probability'] = np.nan
        b.dp = types.SimpleNamespace(get_analyzed_dataframe=lambda *_: (d, None), current_whitelist=lambda: ['BTC/USDT', 'ETH/USDT'])
        return b
    def entry(self, b, amount, pair='BTC/USDT'):
        return b.confirm_trade_entry(pair, 'limit', amount, 100., 'GTC', self.now, None, 'long')
    def count(self, where='1=1'):
        c = sqlite3.connect(self.root / 'paper_audit.sqlite')
        try: return c.execute('SELECT COUNT(*) FROM decisions WHERE ' + where).fetchone()[0]
        finally: c.close()

    def test_defaults_unchanged(self):
        b = self.bot()
        self.assertEqual((b.limit_stake, b.limit_open, b.limit_daily_loss, b.limit_approvals), (50., 3, 50., 10))
    def test_limits_follow_standard_config_keys(self):
        b = self.bot(max_open_trades=50, stake_amount=30)
        self.assertEqual((b.limit_stake, b.limit_open, b.limit_daily_loss, b.limit_approvals), (30., 50, 150., 200))
    def test_unlimited_and_invalid_values_fall_back(self):
        b = self.bot(max_open_trades=-1, stake_amount='unlimited')
        self.assertEqual((b.limit_stake, b.limit_open), (50., 3))
        b = self.bot(max_open_trades='x', stake_amount=0)
        self.assertEqual((b.limit_stake, b.limit_open), (50., 3))
    def test_stake_limit_uses_configured_stake(self):
        b = self.bot(max_open_trades=50, stake_amount=30)
        self.assertTrue(self.entry(b, .30))            # 30 USDT = mörkin
        self.assertFalse(self.entry(b, .31))           # 31 USDT > 30
        self.assertEqual(b.custom_stake_amount('BTC/USDT', self.now, 100., 100., 5., 1000., 1, None, 'long'), 30.)
    def test_open_trade_limit_is_50(self):
        b = self.bot(max_open_trades=50, stake_amount=30)
        Trade.rows = [types.SimpleNamespace(is_open=True) for _ in range(49)]
        self.assertTrue(self.entry(b, .30))            # 49 opnar -> pláss fyrir 50.
        Trade.rows = [types.SimpleNamespace(is_open=True) for _ in range(50)]
        self.assertFalse(self.entry(b, .30, 'ETH/USDT'))
    def test_daily_approvals_scale_with_slots(self):
        b = self.bot(max_open_trades=50, stake_amount=30)
        for i in range(200): self.assertTrue(self.entry(b, .30), i)
        self.assertFalse(self.entry(b, .30))           # 201. kaup á deginum
    def test_daily_loss_limit_scales(self):
        b = self.bot(max_open_trades=50, stake_amount=30)
        Trade.rows = [types.SimpleNamespace(is_open=False, close_date_utc=self.now, close_profit_abs=-149.)]
        self.assertTrue(self.entry(b, .30))            # -149 > -150
        Trade.rows = [types.SimpleNamespace(is_open=False, close_date_utc=self.now, close_profit_abs=-150.)]
        self.assertFalse(self.entry(b, .30, 'ETH/USDT'))
    def test_repeated_rejection_logged_once_per_candle(self):
        b = self.bot()
        for _ in range(25): self.assertFalse(self.entry(b, .9))                  # stake_limit í hverri lykkju
        self.assertEqual(self.count('accepted=0'), 1)
        self.assertFalse(self.entry(b, .9, 'ETH/USDT'))                           # annað par -> önnur skráning
        self.assertEqual(self.count('accepted=0'), 2)
        Trade.rows = [types.SimpleNamespace(is_open=True) for _ in range(3)]      # önnur ástæða á sama pari -> skráð
        self.assertFalse(self.entry(b, .5)); self.assertEqual(self.count('accepted=0'), 3)
    def test_accepted_always_logged_and_resets_dedup(self):
        b = self.bot(max_open_trades=50, stake_amount=30)
        self.assertFalse(self.entry(b, .9)); self.assertFalse(self.entry(b, .9))
        self.assertEqual(self.count('accepted=0'), 1)
        self.assertTrue(self.entry(b, .3)); self.assertTrue(self.entry(b, .3))
        self.assertEqual(self.count('accepted=1'), 2)                             # samþykki er ALLTAF skráð
        self.assertFalse(self.entry(b, .9))                                       # höfnun eftir samþykki -> skráð aftur
        self.assertEqual(self.count('accepted=0'), 2)
    def test_candle_written_once_not_every_loop(self):
        b = self.bot(); calls = []
        real = b._db
        b._db = lambda: (calls.append(1), real())[1]
        b.bot_loop_start(self.now); first = len(calls)
        for _ in range(20): b.bot_loop_start(self.now)
        self.assertEqual(len(calls), first)                                       # engin ný gagnagrunnstenging
        c = sqlite3.connect(self.root / 'paper_audit.sqlite')
        self.assertEqual(c.execute('SELECT COUNT(*) FROM candles').fetchone()[0], 2); c.close()

if __name__ == '__main__': unittest.main()
