from engine import *
rows={x['signature']:x for x in json.loads((ROOT/'selected.json').read_text())};points=json.loads((ROOT/'price_points.json').read_text());idx={(x['signature'],x['seconds']):i for i,x in enumerate(points)};imports=[];comparisons=[]
for x in json.loads((ROOT.parent/'solana_three_check/results.json').read_text()):
 key=(x['buy'],x['seconds'])
 if key not in idx or x['status']!='ok':continue
 current=points[idx[key]]
 if current['status']=='ok':comparisons.append({'signature':key[0],'seconds':key[1],'difference_pct_points':str(D(current['growth_pct'])-D(x['growth_pct']))});continue
 status='quote_freshness_unverified' if x['n']==0 and x['seconds']==900 else 'ok'
 pool=next(r['route'][0]['pool'] for r in json.loads((ROOT/'routes.json').read_text()) if r['signature']==key[0]);primary={'pool':pool,'slot':x['primary_slot'],'time':x['primary_time'],'age_seconds':x['target']-x['primary_time']}
 r=dict(signature=key[0],seconds=key[1],target=x['target'],status=status,price_usdc=x['price_usdc'],growth_pct=str((D(x['price_usdc'])/D(rows[key[0]]['entry_usdc'])-1)*100),legs=[primary]+x['quote_observations'],source='previous_three_tokens_calculation',prior_reported_growth_pct=x['growth_pct'])
 points[idx[key]]=r;imports.append({'signature':key[0],'seconds':key[1],'status':status})
ws='2NkPm8GfVw2FYBHrbLbhUrwJGmGECYh4t89oMdCnEsnAXGK8qu4BoTfpNVKEHZLWgBfGYCpZ2oUCBFLW35m8XCNS'
if ws in rows:
 for x in json.loads((ROOT.parent/'solana_wow_check/price_results.json').read_text()):
  key=(ws,x['seconds'])
  if points[idx[key]]['status']=='ok':continue
  legs=[{'pool':'LeZ2DH1y7bqzAghYbXqKB6fNxoBKqPSPihv3EmkLBRv','slot':x['slot'],'time':x['observed_time'],'age_seconds':x['target']-x['observed_time']},{'pool':'78ReVNMLGRWmjtf2HmBoHUe2pRcsctXTTbxJnbhchyze','slot':x['quote_slot'],'time':x['quote_time'],'age_seconds':x['target']-x['quote_time']}]
  points[idx[key]]=dict(signature=ws,seconds=x['seconds'],target=x['target'],status='ok',price_usdc=x['price_usdc'],growth_pct=str((D(x['price_usdc'])/D(rows[ws]['entry_usdc'])-1)*100),legs=legs,source='previous_WOW_calculation',prior_reported_growth_pct=x['growth_pct']);imports.append({'signature':ws,'seconds':x['seconds'],'status':'ok'})
(ROOT/'price_points.json').write_text(json.dumps(points,indent=2));(ROOT/'prior_reconciliation.json').write_text(json.dumps({'imported':imports,'overlapping_checks':comparisons},indent=2));print('imported',len(imports),'overlap',len(comparisons))
