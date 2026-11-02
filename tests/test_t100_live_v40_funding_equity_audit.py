"""Synthetic trace verifies funding uses prior marks before same-bar exit."""
import csv
import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'docs/evidence/issue-14/rebuild_funding_equity.py'
spec = importlib.util.spec_from_file_location('funding_audit', MODULE_PATH)
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


class FundingEquityAuditTests(unittest.TestCase):
    def test_prior_mark_and_funding_before_same_bar_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / 'historical-market-data-min30-v1'
            directory.mkdir()
            start = '2026-01-01T00:00:00+00:00'
            close = '2026-01-01T01:00:00+00:00'
            epoch = int(datetime.fromisoformat(start).timestamp())
            with sqlite3.connect(directory / 'historical-market-data-min30-v1.sqlite') as db:
                db.execute('CREATE TABLE candles(symbol TEXT, interval TEXT, open_ts INTEGER, close REAL)')
                db.executemany('INSERT INTO candles VALUES(?,?,?,?)',
                               [('TEST_USDT','Min30',epoch,10), ('TEST_USDT','Min30',epoch+1800,8.5),
                                ('TEST_USDT','Min30',epoch+3600,1)])
            with sqlite3.connect(root / 'mexc-certification-cache-v1.sqlite') as db:
                db.execute('CREATE TABLE funding(symbol TEXT, settle_ts INTEGER, rate REAL)')
                db.executemany('INSERT INTO funding VALUES(?,?,?)',
                               [('TEST_USDT',epoch,0.9), ('TEST_USDT',epoch+3600,0.01)])
            rows = {name: [] for name in ('entries','exits','fills','metrics')}
            for cost, pnl, final in ((100,'9.028','2008.798'), (200,'8.128','2006.898')):
                base = {'scenario':f'V32_VOL30_SKIP_H4_S{cost}_CAP5_V39_HYBRID'}
                rows['entries'].append(dict(base,symbol='TEST_USDT',time=start,notional='100'))
                rows['exits'].append(dict(base,symbol='TEST_USDT',entry_time=start,exit_time=close,
                                         notional='100',pnl_net_before_funding=pnl))
                rows['fills'].append(dict(base,symbol='TEST_USDT',time=start,kind='entry',price='10'))
                rows['metrics'].append(dict(base,funding_net='0.85',funding_events='1',final_equity=final))
            for name, values in rows.items():
                with (root / (name+'.csv')).open('w') as file:
                    writer=csv.DictWriter(file,fieldnames=list(values[0]));writer.writeheader();writer.writerows(values)
            result = audit_module.audit(root, {'baseline':root,'filtered':root})
            for report in result.values():
                self.assertEqual(report['funding_events'],1)
                self.assertEqual(Decimal(report['funding_difference']),0)
                self.assertEqual(Decimal(report['final_equity_difference']),0)
