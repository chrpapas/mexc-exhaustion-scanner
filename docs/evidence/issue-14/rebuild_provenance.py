import csv, hashlib, json, platform, subprocess, sys
from pathlib import Path
from zipfile import ZipFile
from scripts.v40_api_filtered_replay_compare import _frozen_row_matches
from scripts.v40_backtest_api_coverage import _csv_from_zip, scenario_label
root=Path.cwd(); out=root/'docs/evidence/issue-14'
sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)
 return h.hexdigest()
archive=Path('/Users/chrpapas/v40_parity_for_live.zip')
source=Path('/Users/chrpapas/Downloads/t100_chronological_contract_lots_v39_1/contract_replay_v40_scale.py')
manifest=Path('/Users/chrpapas/trader-backtest/v40_api_prefilter_20261101.json')
comparison={}
with ZipFile(archive) as z:
 for cost in (100,200):
  scenario=scenario_label(cost,'HYBRID'); details={}
  for name in ('entries.csv','metrics.csv','gate_decisions.csv'):
   expected=[r for r in _csv_from_zip(z,'capital_2000/'+name) if r['scenario']==scenario]
   actual=[r for r in csv.DictReader((Path('/private/tmp/t100-issue14-baseline-20261102')/name).open()) if r['scenario']==scenario]
   details[name]={'rows':len(expected),'all_frozen_fields_match':len(actual)==len(expected) and all(_frozen_row_matches(a,e) for a,e in zip(actual,expected))}
  comparison[str(cost)]=details
assert all(v['all_frozen_fields_match'] for d in comparison.values() for v in d.values())
(out/'baseline-two-cost-frozen-fields.json').write_text(json.dumps(comparison,indent=2)+'\n')
files={}
for label,folder in [('fresh_baseline',Path('/private/tmp/t100-issue14-baseline-20261102')),('fresh_filtered',Path('/private/tmp/t100-issue14-filtered-20261102'))]:
 files[label]={p.name:digest(p) for p in sorted(folder.iterdir()) if p.is_file()}
provenance={'date':'2026-11-02','code_revision':sha,'fetched_handoff_revision':'2669da62c6fc5bd7d7b25b1c681197914fedc629','python_version':platform.python_version(),'original_simulator_sha256':digest(source),'frozen_archive_sha256':digest(archive),'public_filter_sha256':digest(manifest),'original_module_hashes':{p.name:digest(p) for p in sorted(source.parent.glob('*.py'))},'fresh_replay_output_sha256':files,'entry_allowed':False,'live_authorization':False,'mexc_orders_sent':False,'boxwave_modified':False,'limitations':['Public filter is dated 2026-11-01; no current account permissions inferred.','Funding source warns WAL format without WAL file; prior uncheckpointed transactions cannot be recovered.','Original simulator independently rerun; simulator source itself is not an independent implementation.','Frozen archive contains entries, gate decisions and metrics, not full exit/fill paths.','Private credentials, DATABASE_URL, Docker and postgres executable unavailable in this environment.']}
(out/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
