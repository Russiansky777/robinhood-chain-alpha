from pathlib import Path
import json,statistics,collections,datetime
P=Path(__file__).parent;rows=json.loads((P/'selected.json').read_text());points=json.loads((P/'price_points.json').read_text());fees=json.loads((P/'fees.json').read_text());ix={(x['signature'],x['seconds']):x for x in points};H=[5,15,30,60,180,300,900,3600,21600,86400];short=[5,15,30,60,180,300]
full=[r for r in rows if all(ix[(r['signature'],s)]['status']=='ok' for s in short)]
def describe(values):
 return {'n':len(values),'mean':statistics.mean(values) if values else None,'median':statistics.median(values) if values else None,'positive_share':sum(v>0 for v in values)/len(values) if values else None,'at_least_5_share':sum(v>=.05 for v in values)/len(values) if values else None,'minimum':min(values) if values else None,'maximum':max(values) if values else None}
stats=[];cohort=[]
for name in ['all','zero','add']:
 group=[r for r in rows if name=='all' or r['zero_balance']==(name=='zero')]
 for s in H:
  pp=[ix[(r['signature'],s)] for r in group];v=[float(x['growth_pct'])/100 for x in pp if x['status']=='ok'];stats.append(dict(group=name,seconds=s,selected=len(group),future=sum(x['status']=='future' for x in pp),missing=sum(x['status'] not in ['ok','future'] for x in pp),**describe(v)))
 group=[r for r in full if name=='all' or r['zero_balance']==(name=='zero')]
 for s in short:
  gross=[float(ix[(r['signature'],s)]['growth_pct'])/100 for r in group];late=[float(ix[(r['signature'],s)]['price_usdc'])/float(ix[(r['signature'],5)]['price_usdc'])-1 for r in group];cohort.append({'group':name,'seconds':s,'leader':describe(gross),'after_5s':describe(late)})
meta={'selected':len(rows),'zero':sum(r['zero_balance'] for r in rows),'add':sum(not r['zero_balance'] for r in rows),'unique_mints':len({r['mint'] for r in rows}),'full_5min':len(full),'full_5min_zero':sum(r['zero_balance'] for r in full),'full_5min_signatures':[r['signature'] for r in full],'point_status':dict(collections.Counter(x['status'] for x in points)),'asof_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'start_utc':datetime.datetime.fromtimestamp(min(r['time'] for r in rows),datetime.timezone.utc).isoformat(),'end_utc':datetime.datetime.fromtimestamp(max(r['time'] for r in rows),datetime.timezone.utc).isoformat(),'sum_usdc':sum(float(r['usdc_spent']) for r in rows),'mint_concentration':dict(collections.Counter(r['mint'] for r in rows))}
(P/'analysis.json').write_text(json.dumps({'meta':meta,'stats':stats,'cohort':cohort},indent=2));print(json.dumps(meta,ensure_ascii=False))
