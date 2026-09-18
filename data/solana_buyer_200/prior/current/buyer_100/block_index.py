from rpc import rpc
from engine import *
import threading,gzip,collections
(ROOT/'block_events').mkdir(exist_ok=True)
locks={};guard=threading.Lock()
def summary(slot):
 with guard:lock=locks.setdefault(slot,threading.Lock())
 with lock:
  f=ROOT/'block_events'/f'{slot}_v3.json'
  if f.exists():return json.loads(f.read_text())
  b=None
  for folder in ['solana_three_check','solana_wow_check']:
   old=ROOT.parent/folder/f'block_{slot}.json'
   if old.exists():
    b=json.loads(old.read_text()).get('result')
    if b:break
  if b is None:b=rpc('getBlock',[slot,{'encoding':'json','transactionDetails':'full','rewards':False,'maxSupportedTransactionVersion':1}],f'block_{slot}')
  if b is None:raise RuntimeError('null historical block '+str(slot))
  events={};unknown={}
  for idx,tx in enumerate(b['transactions']):
   if tx['meta']['err']:continue
   ev=decode_tx(tx);cnt=collections.Counter(e['pool'] for e in ev)
   for e in ev:events.setdefault(e['pool'],[]).append([idx,tx['transaction']['signatures'][0],e])
   for pool,n in expected_pool_counts(tx).items():
    if n>cnt[pool]:unknown.setdefault(pool,[]).append(idx)
  out={'slot':slot,'time':b['blockTime'],'events':events,'unknown':unknown};f.write_text(json.dumps(out));return out
