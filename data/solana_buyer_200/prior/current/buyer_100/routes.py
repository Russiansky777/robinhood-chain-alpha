from engine import *
from collections import deque
import sqlite3
META=json.loads((ROOT.parent/'solana_three_check/pool_meta.json').read_text())
def plan_routes():
 rows=json.loads((ROOT/'selected.json').read_text());txevents={};meta=dict(META)
 con=sqlite3.connect(ROOT/'events.sqlite')
 for _,data in con.execute('SELECT COUNT(*), data FROM events GROUP BY pool ORDER BY COUNT(*) DESC'):
  e=json.loads(data)
  if e.get('m0') and e.get('m1') and e['pool'] not in meta:meta[e['pool']]={k:e[k] for k in ['kind','m0','m1','d0','d1','config'] if k in e}
 con.close()
 for r in rows:
  tx=json.loads((ROOT/'tx'/f"{r['signature']}.json").read_text())['result'];ev=decode_tx(tx);txevents[r['signature']]=ev
  for e in ev:
   if e.get('m0') and e.get('m1'):meta[e['pool']]={**meta.get(e['pool'],{}),**{k:e[k] for k in ['kind','m0','m1','d0','d1','config'] if k in e}}
 out=[]
 for r in rows:
  ev=txevents[r['signature']];target=[e for e in ev if r['mint'] in [e.get('m0'),e.get('m1')]]
  def output_size(e):
   if e['kind']=='cp':return e['event']['output_amount'] if e['event']['output_mint']==r['mint'] else 0
   return 0
  target.sort(key=output_size,reverse=True)
  path=None
  for primary in target:
   quote=primary['m1'] if primary['m0']==r['mint'] else primary['m0'];p0=[{'pool':primary['pool'],'from':r['mint'],'to':quote}]
   q=deque([(quote,p0,{r['mint'],quote})])
   while q:
    mint,p,seen=q.popleft()
    if mint==USDC:path=p;break
    if len(p)>=3:continue
    # Prefer pools actually used by leader; remaining official known pools provide fallback.
    poolids=list(dict.fromkeys([e['pool'] for e in ev]+list(meta)))
    for pool in poolids:
     m=meta.get(pool,{})
     if mint not in [m.get('m0'),m.get('m1')]:continue
     other=m['m1'] if m['m0']==mint else m['m0']
     if other in seen:continue
     q.append((other,p+[{'pool':pool,'from':mint,'to':other}],seen|{other}))
   if path:break
  out.append({'signature':r['signature'],'route':path,'status':'planned' if path else 'route_decoder_missing'})
 (ROOT/'routes.json').write_text(json.dumps(out,indent=2));(ROOT/'route_meta.json').write_text(json.dumps(meta,indent=2));print('routes',sum(bool(x['route']) for x in out),'of',len(rows))
if __name__=='__main__':plan_routes()
