"""Historical V40 funding and final equity reconciliation of prescribed trades.

Read-only local research; no MEXC orders, credentials, execution or risk permits.
The original funding/equity arithmetic is unchanged. --immutable-candles is
an explicit opt-in only for verified, dormant historical SQLite copies.
Output is NOT full strategy or live execution certification.
"""

import csv,sqlite3,json
from pathlib import Path
from datetime import datetime
from decimal import Decimal as D
end=int(datetime.fromisoformat('2026-09-25T08:30:00+00:00').timestamp())
def ts(s):return int(datetime.fromisoformat(s).timestamp())
def db(p, *, immutable=False):
 p=Path(p).expanduser().resolve()
 if not p.is_file():
  raise ValueError('HISTORICAL_DATABASE_NOT_FOUND')
 # The funding snapshot has always used immutable reads. When selected for
 # the copied candle database, fail closed on pending transaction side files.
 immutable=immutable or p.name=='mexc-certification-cache-v1.sqlite'
 if immutable and any(Path(str(p)+suffix).exists() for suffix in ('-wal','-journal')):
  raise ValueError('IMMUTABLE_DATABASE_PENDING_JOURNAL')
 suffix='?mode=ro'+('&immutable=1' if immutable else '')
 c=sqlite3.connect(p.as_uri()+suffix,uri=True)
 try:
  c.execute('PRAGMA query_only=ON')
  c.execute('BEGIN')
 except BaseException:
  c.close()
  raise
 return c
def audit(root, folders, *, candles_path=None, funding_path=None, immutable_candles=False):
 candles=db(candles_path or root/'historical-market-data-min30-v1/historical-market-data-min30-v1.sqlite',immutable=immutable_candles)
 funds=db(funding_path or root/'mexc-certification-cache-v1.sqlite',immutable=True)
 result={}
 for cohort,p in folders.items():
  raw={n:list(csv.DictReader((p/(n+'.csv')).open())) for n in ('entries','exits','fills','metrics')}
  for cost in (100,200):
   scenario=f'V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID';data={n:[r for r in rows if r['scenario']==scenario] for n,rows in raw.items()};m=data['metrics'][0];funding=D(0);events=0;unrealized=D(0)
   for e in data['entries']:
    symbol=e['symbol'];entered=ts(e['time']);notional=D(e['notional']);entryfills=[f for f in data['fills'] if f['symbol']==symbol and f['time']==e['time'] and f['kind']=='entry'];assert len(entryfills)==1;price=D(entryfills[0]['price'])
    exits=sorted([x for x in data['exits'] if x['symbol']==symbol and x['entry_time']==e['time']],key=lambda x:ts(x['exit_time']))
    for settle,rate in funds.execute('SELECT settle_ts,rate FROM funding WHERE symbol=? AND settle_ts>? AND settle_ts<=? ORDER BY settle_ts',(symbol,entered,end)):
     apply=(settle+1799)//1800*1800
     if apply>end:continue
     remaining=notional-sum((D(x['notional']) for x in exits if ts(x['exit_time'])<apply),D(0))
     if remaining<=D('.001'):continue
     row=candles.execute("SELECT close FROM candles WHERE symbol=? AND interval='Min30' AND open_ts>=? AND open_ts<? ORDER BY open_ts DESC LIMIT 1",(symbol,entered,apply)).fetchone();assert row
     funding+=remaining/price*D(str(row[0]))*D(str(rate));events+=1
    remaining=notional-sum((D(x['notional']) for x in exits),D(0))
    if remaining>D('.001'):
     row=candles.execute("SELECT close FROM candles WHERE symbol=? AND interval='Min30' AND open_ts>=? AND open_ts<=? ORDER BY open_ts DESC LIMIT 1",(symbol,entered,end)).fetchone();assert row
     unrealized+=remaining*(1-D(str(row[0]))/price)
   entryfee=sum((D(e['notional'])*(D('.0008')+D(cost)/10000) for e in data['entries']),D(0))
   pnl=sum((D(x['pnl_net_before_funding']) for x in data['exits']),D(0))
   final=D(2000)-entryfee+pnl+funding+unrealized
   result[f'{cohort}_{cost}']={'funding_difference':str(funding-D(m['funding_net'])),'funding_events':events,'reported_events':m['funding_events'],'final_equity_difference':str(final-D(m['final_equity'])),'recomputed_final_equity':str(final)}
 candles.close();funds.close()
 return result

if __name__ == "__main__":
 import argparse
 parser=argparse.ArgumentParser(description='Offline funding and final equity reconciliation of existing V40 historical CSV ledgers. No orders or simulator execution.')
 parser.add_argument('--data-root',type=Path,default=Path.home()/'trader-backtest')
 parser.add_argument('--folder',type=Path,help='One original backtest output folder containing entries/exits/fills/metrics CSVs')
 parser.add_argument('--candles',type=Path,help='Explicit verified historical Min30 SQLite database')
 parser.add_argument('--funding',type=Path,help='Explicit copied historical funding SQLite cache')
 parser.add_argument('--immutable-candles',action='store_true',help='Only for stable offline SQLite copy with no -wal/-journal side files')
 args=parser.parse_args()
 root=args.data_root.expanduser()
 folders={'historical':args.folder.expanduser()} if args.folder else {
  cohort:Path("/private/tmp/t100-issue14-"+cohort+"-20261102")
  for cohort in ("baseline","filtered")
 }
 report=audit(root,folders,candles_path=args.candles,funding_path=args.funding,
              immutable_candles=args.immutable_candles)
 print(json.dumps(report,indent=2))
