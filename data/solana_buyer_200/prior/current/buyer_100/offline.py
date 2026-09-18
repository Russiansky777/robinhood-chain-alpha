import rpc
rpc.OFFLINE=True
from price_batch import *
rows=json.loads((ROOT/'selected.json').read_text());routes={x['signature']:x['route'] for x in json.loads((ROOT/'routes.json').read_text())};old={(x['signature'],x['seconds']):x for x in json.loads((ROOT/'price_points.json').read_text())};out=[]
for row in rows:
 for sec in HORIZONS:
  key=(row['signature'],sec)
  if key in old and old[key]['status']=='ok':res=old[key]
  else:
   try:res=calculate(row,sec,routes.get(row['signature']))
   except Exception as e:res={'signature':row['signature'],'seconds':sec,'target':row['time']+sec,'status':'data_not_cached','missing':str(e)}
  out.append(res)
(ROOT/'price_points.json').write_text(json.dumps(out,indent=2));print('offline',len(out),dict(collections.Counter(x['status'] for x in out)))
