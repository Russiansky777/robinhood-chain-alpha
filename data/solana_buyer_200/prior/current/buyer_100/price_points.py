from rpc import rpc
from engine import *
import time
from block_index import summary as block_summary
HORIZONS=[5,15,30,60,180,300,900,3600,21600,86400]
(ROOT/'history').mkdir(exist_ok=True);(ROOT/'accounts').mkdir(exist_ok=True);(ROOT/'observations').mkdir(exist_ok=True)
META=json.loads((ROOT/'route_meta.json').read_text())
H=[]
for f in sorted(ROOT.glob('wallet_history_*.json')):
 try:H+=json.loads(f.read_text())['result']
 except Exception:pass

def price_event(e):
 if e.get('p1_per_0') is not None:return e
 m=META.get(e['pool'],{})
 if e['kind']=='cl' and m.get('d0') is not None and m.get('d1') is not None:
  e.update(m0=m['m0'],m1=m['m1'],d0=m['d0'],d1=m['d1'],p1_per_0=str((D(e['sqrt'])/D(2**64))**2*D(10)**(m['d0']-m['d1'])),status='ok');return e
 if e['kind']=='launch':
  a=rpc('getAccountInfo',[e['config'],{'encoding':'base64'}],'accounts/'+e['config'])
  if not a.get('value'):return e
  raw=base64.b64decode(a['value']['data'][0]);e['curve_type']=raw[16]
  if raw[16]!=0:e['status']='unsupported_launch_curve';return e
  v=e['event']
  if v['pool_status']!=0:e['status']='launch_migration_requires_new_pool';return e
  if e.get('d0') is None or e.get('d1') is None:return e
  e.update(p1_per_0=str(D(v['virtual_quote']+v['real_quote_after'])/D(v['virtual_base']-v['real_base_after'])*D(10)**(e['d0']-e['d1'])),status='ok')
  return e
 if e['kind']=='dl':
  if 'step' not in m:
   a=rpc('getAccountInfo',[e['pool'],{'encoding':'base64'}],'accounts/'+e['pool'])
   if not a.get('value'):return e
   raw=base64.b64decode(a['value']['data'][0]);m.update(step=struct.unpack_from('<H',raw,80)[0],m0=b58(raw[88:120]),m1=b58(raw[120:152]));META[e['pool']]=m
  d0=e.get('d0',m.get('d0'));d1=e.get('d1',m.get('d1'))
  if d0 is None or d1 is None:return e
  e.update(m0=m['m0'],m1=m['m1'],d0=d0,d1=d1,p1_per_0=str((D(1)+D(m['step'])/10000)**e['event']['end_bin']*D(10)**(d0-d1)),status='ok')
 return e

def point(pool,t):
 f=ROOT/'observations'/f'{pool}_{t}.json'
 if f.exists():
  cached=json.loads(f.read_text())
  if cached['status']!='30_blocks_without_decoded_swap':return cached
 after=[h for h in H if h.get('blockTime') and h['blockTime']>t]
 anchor=min(after,key=lambda h:(h['blockTime'],h['slot'],h.get('transactionIndex',0))) if after else None
 opts={'limit':1000}
 if anchor:opts['before']=anchor['signature']
 scanned=0;result=None;seen_slots=set()
 for page in range(30):
  name=hashlib.sha256(json.dumps([pool,opts],sort_keys=True).encode()).hexdigest()[:24]
  hist=rpc('getSignaturesForAddress',[pool,opts],'history/'+name)
  for h in hist:
   if h['err'] is not None or h.get('blockTime') is None or h['blockTime']>t:continue
   if h['slot'] in seen_slots:continue
   seen_slots.add(h['slot']);scanned+=1
   slot=h['slot'];bs=block_summary(slot);es=bs['events'].get(pool,[]);unknown=bs['unknown'].get(pool,[])
   if unknown and (not es or max(unknown)>=es[-1][0]):
    result=dict(pool=pool,target=t,slot=slot,status='missing_swap_event',indices=unknown);break
   if es:
    idx,sig,e=es[-1];e=price_event(e);result=dict(pool=pool,target=t,slot=slot,index=idx,time=bs['time'],age_seconds=t-bs['time'],signature=sig,scanned_blocks=scanned,event=e,status=e['status']);break
   if scanned>=120:result=dict(pool=pool,target=t,status='120_blocks_without_decoded_swap',scanned_blocks=scanned);break
  if result:break
  if len(hist)<opts['limit']:
   result=dict(pool=pool,target=t,status='no_historical_swap',scanned_references=scanned);break
  opts['before']=hist[-1]['signature']
 if result is None:result=dict(pool=pool,target=t,status='history_pagination_limit',scanned_references=scanned)
 f.write_text(json.dumps(result,indent=2));return result

if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--max-cases',type=int,default=100);args=parser.parse_args()
 rows=json.loads((ROOT/'selected.json').read_text())[:args.max_cases];routes={x['signature']:x['route'] for x in json.loads((ROOT/'routes.json').read_text())};out=[]
 for row in rows:
  for sec in HORIZONS:
   t=row['time']+sec;res=dict(signature=row['signature'],seconds=sec,target=t)
   if t>int(time.time()):res['status']='future'
   elif not routes.get(row['signature']):res['status']='route_decoder_missing'
   else:
    value=D(1);legs=[]
    for leg in routes[row['signature']]:
     p=point(leg['pool'],t);legs.append(p)
     if p['status']!='ok':res['status']=p['status'];break
     e=p['event'];v=D(e['p1_per_0'])
     if leg['from']==e['m0'] and leg['to']==e['m1']:value*=v
     elif leg['from']==e['m1'] and leg['to']==e['m0']:value/=v
     else:res['status']='mint_mismatch';break
    else:res.update(status='ok',price_usdc=str(value),growth_pct=str((value/D(row['entry_usdc'])-1)*100))
    res['legs']=legs
   out.append(res);(ROOT/'price_points.json').write_text(json.dumps(out,indent=2));print('price',len(out),'case',len(out)//10+1,sec,res['status'],flush=True)
 (ROOT/'route_meta.json').write_text(json.dumps(META,indent=2))
