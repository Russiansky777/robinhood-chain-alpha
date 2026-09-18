from rpc import *
from decimal import Decimal as D
W='Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit';out=ROOT/'tx';out.mkdir(exist_ok=True)
seen=set();count=0
for folder in ['solana_wow_check','solana_three_check']:
 for f in (ROOT.parent/folder).glob('block_*.json'):
  if not f.stem.split('_')[1].isdigit() or f.stem in seen:continue
  seen.add(f.stem);b=json.loads(f.read_text()).get('result')
  if not b:continue
  for idx,r in enumerate(b['transactions']):
   if not any(k.get('signer') and k['pubkey']==W for k in r['transaction']['message']['accountKeys']):continue
   r=dict(r,slot=int(f.stem.split('_')[1]),blockTime=b['blockTime'],transactionIndex=idx);sig=r['transaction']['signatures'][0];p=out/(sig+'.json')
   if not p.exists():p.write_text(json.dumps({'result':r}));count+=1
 for f in (ROOT.parent/folder).glob('tx_*.json'):
  x=json.loads(f.read_text());r=x.get('result')
  if r:
   p=out/(r['transaction']['signatures'][0]+'.json')
   if not p.exists():p.write_text(json.dumps(x));count+=1
print('blocks scanned',len(seen),'wallet tx cached',count,flush=True)
