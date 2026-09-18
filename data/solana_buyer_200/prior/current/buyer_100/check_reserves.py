from engine import *
import gzip
out=[];count=0
files=[]
for folder in ['solana_three_check','solana_wow_check','buyer_100']:
 for f in (ROOT.parent/folder).glob('block_*.json*'):
  suffix=f.name[6:].split('.')[0]
  if suffix.isdigit():files.append((int(suffix),f))
for slot,f in files:
 try:b=json.loads(gzip.decompress(f.read_bytes()) if f.suffix=='.gz' else f.read_text()).get('result')
 except Exception:continue
 if not b or 'transactions' not in b:continue
 prev={}
 for idx,r in enumerate(b['transactions']):
  if r['meta']['err']:continue
  for e in decode_tx(r):
   if e['kind']!='cp' or e['status']!='ok':continue
   v=e['event'];before={v['input_mint']:v['input_vault_before'],v['output_mint']:v['output_vault_before']};pool=e['pool']
   if pool in prev:
    old=prev[pool];count+=1
    if old['reserves']!=before:out.append({'slot':slot,'pool':pool,'prev_index':old['index'],'index':idx,'prev_after':old['reserves'],'next_before':before})
   ri=v['input_vault_before']+v['input_amount']-v['trade_fee']*120000//1000000-v['trade_fee']*40000//1000000-(v['creator_fee'] if v['creator_fee_on_input'] else 0)
   ro=v['output_vault_before']-v['output_amount']-(0 if v['creator_fee_on_input'] else v['creator_fee'])
   prev[pool]={'index':idx,'reserves':{v['input_mint']:ri,v['output_mint']:ro}}
result={'checked_transitions':count,'mismatches':out,'blocks_examined':len(files),'note':'Non-swap liquidity changes can explain mismatches; each mismatch requires inspection.'}
(ROOT/'reserve_checks.json').write_text(json.dumps(result,indent=2));print('transitions',count,'mismatches',len(out),'blocks',len(files))
