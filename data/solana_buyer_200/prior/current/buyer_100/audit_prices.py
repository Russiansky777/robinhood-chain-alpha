from engine import *
import gzip,collections
points=json.loads((ROOT/'price_points.json').read_text());checks=[]
blocks={}
for point in points:
 for leg in point.get('legs',[]):
  if leg.get('status')!='ok':continue
  slot=leg['slot'];pool=leg['pool'];key=(slot,pool)
  if key in blocks:continue
  b=None
  for folder in ['buyer_100','solana_three_check','solana_wow_check']:
   for ext in ['json','json.gz']:
    f=ROOT.parent/folder/f'block_{slot}.{ext}'
    if f.exists():
     b=json.loads(gzip.decompress(f.read_bytes()) if ext.endswith('gz') else f.read_text()).get('result')
     if b:break
   if b:break
  if not b:checks.append(dict(slot=slot,pool=pool,status='block_not_cached'));continue
  unknown=[];last=None
  for idx,tx in enumerate(b['transactions']):
   if tx['meta']['err']:continue
   expected=expected_swaps(tx,pool)
   if not expected:continue
   decoded=[e for e in decode_tx(tx) if e['pool']==pool]
   if decoded:last=idx
   if expected>len(decoded):unknown.append(dict(index=idx,expected=expected,decoded=len(decoded),signature=tx['transaction']['signatures'][0]))
  newer=[x for x in unknown if last is None or x['index']>=last]
  result=dict(slot=slot,pool=pool,status='missing_swap_event' if newer else 'ok',last_decoded_index=last,missing_after_last=newer);checks.append(result);blocks[key]=result
(ROOT/'price_event_audit.json').write_text(json.dumps(checks,indent=2));print(dict(collections.Counter(x['status'] for x in checks)))
