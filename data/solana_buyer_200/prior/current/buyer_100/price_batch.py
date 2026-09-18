from price_points import *
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
import argparse,collections,threading
locks={};guard=threading.Lock()
def safe_point(pool,t):
 with guard:lock=locks.setdefault((pool,t),threading.Lock())
 with lock:return point(pool,t)
def calculate(row,sec,route):
 t=row['time']+sec;res=dict(signature=row['signature'],seconds=sec,target=t)
 if t>int(time.time()):res['status']='future'
 elif not route:res['status']='route_decoder_missing'
 else:
  value=D(1);legs=[]
  for leg in route:
   p=safe_point(leg['pool'],t);legs.append(p)
   if p['status']!='ok':res['status']=p['status'];break
   e=p['event'];v=D(e['p1_per_0'])
   if leg['from']==e['m0'] and leg['to']==e['m1']:value*=v
   elif leg['from']==e['m1'] and leg['to']==e['m0']:value/=v
   else:res['status']='mint_mismatch';break
  else:res.update(status='ok',price_usdc=str(value),growth_pct=str((value/D(row['entry_usdc'])-1)*100))
  res['legs']=legs
 return res
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--max-cases',type=int,default=100);parser.add_argument('--workers',type=int,default=4);args=parser.parse_args()
 rows=json.loads((ROOT/'selected.json').read_text())[:args.max_cases];routes={x['signature']:x['route'] for x in json.loads((ROOT/'routes.json').read_text())};jobs=[(r,s,routes.get(r['signature'])) for r in rows for s in HORIZONS];order={(r['signature'],s):i for i,(r,s,_) in enumerate(jobs)};results={}
 if (ROOT/'price_points.json').exists():
  for x in json.loads((ROOT/'price_points.json').read_text()):
   key=(x['signature'],x['seconds'])
   if key in order and x['status']=='ok':results[key]=x
 pending_jobs=iter(job for job in jobs if (job[0]['signature'],job[1]) not in results)
 def save():
  out=sorted(results.values(),key=lambda x:order[(x['signature'],x['seconds'])]);(ROOT/'price_points.json').write_text(json.dumps(out,indent=2))
 ex=ThreadPoolExecutor(max_workers=args.workers);pending={}
 try:
  for _ in range(args.workers):
   j=next(pending_jobs,None)
   if j:pending[ex.submit(calculate,*j)]=j
  while pending:
   done,_=wait(pending,return_when=FIRST_COMPLETED)
   for future in done:
    j=pending.pop(future);res=future.result();results[(res['signature'],res['seconds'])]=res;save();print('points',len(results),'/',len(jobs),dict(collections.Counter(x['status'] for x in results.values())),flush=True)
    j=next(pending_jobs,None)
    if j:pending[ex.submit(calculate,*j)]=j
 except Exception as e:
  (ROOT/'price_error.json').write_text(json.dumps({'error':str(e),'completed':len(results)},indent=2));save();raise
 finally:ex.shutdown(wait=True,cancel_futures=True)
 save();(ROOT/'route_meta.json').write_text(json.dumps(META,indent=2))
