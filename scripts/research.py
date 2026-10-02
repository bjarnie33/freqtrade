"""Offline tools. No exchange API calls."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'user_data'/'strategies'))
from paper_research import train

p=argparse.ArgumentParser()
s=p.add_subparsers(dest='command',required=True)
t=s.add_parser('train');t.add_argument('csv');t.add_argument('--output',default='user_data/research_models');t.add_argument('--horizon',type=int,default=12)
a=s.add_parser('history');a.add_argument('database');a.add_argument('--output',default='trade_history.json')
c=s.add_parser('compare');c.add_argument('reports',nargs='+')
args=p.parse_args()
if args.command=='train':
    print(train(args.csv,args.output,args.horizon))
elif args.command=='history':
    # Read-only SQLite connection includes the accompanying WAL if present.
    uri=Path(args.database).resolve().as_uri()+'?mode=ro'
    db=sqlite3.connect(uri,uri=True);db.row_factory=sqlite3.Row
    rows=[dict(r) for r in db.execute('SELECT id,pair,strategy,is_open,open_rate,close_rate,open_date,close_date,fee_open,fee_close,fee_open_cost,fee_close_cost,close_profit,close_profit_abs,stake_amount,amount FROM trades ORDER BY open_date,id')]
    Path(args.output).write_text(json.dumps({'trades':rows,'snapshot_warning':'No historical decision features can be recovered from prices alone.'},indent=2))
    print(f'{len(rows)} trades exported')
else:
    reports=[json.loads(Path(path).read_text()) for path in args.reports]
    reference=reports[0]
    for r in reports:
        if any(r[k]!=reference[k] for k in ['data_sha256','test_start','test_end']) or r['simulation']['assumptions']!=reference['simulation']['assumptions']:
            raise ValueError('Comparison requires identical data, test dates and execution costs')
        print(json.dumps({k:r[k] for k in ['model_version','data_sha256','training_end','test_start','test_end','accuracy','log_loss']}))
