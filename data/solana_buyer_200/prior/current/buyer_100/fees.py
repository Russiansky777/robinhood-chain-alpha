import rpc as rpc_module
rpc_module.OFFLINE=True
from rpc import rpc
from engine import *
rows=json.loads((ROOT/'selected.json').read_text());routes={x['signature']:x['route'] for x in json.loads((ROOT/'routes.json').read_text())};out=[]
for row in rows:
 tx=json.loads((ROOT/'tx'/f"{row['signature']}.json").read_text())['result'];ev=decode_tx(tx);route=routes[row['signature']];r={'signature':row['signature'],'status':'unknown_pool_fee','network_fee_sol':row['network_fee_sol']}
 if not route:out.append(r);continue
 pool=route[0]['pool'];matches=[e for e in ev if e['pool']==pool];e=matches[-1];r.update(pool=pool,kind=e['kind'])
 if e['kind']=='cp':
  v=e['event'];r.update(trade_fee_raw=v['trade_fee'],creator_fee_raw=v['creator_fee'],input_transfer_fee_raw=v['input_transfer_fee'],output_transfer_fee_raw=v['output_transfer_fee'],creator_fee_on_input=v['creator_fee_on_input'])
  if e.get('config')==KNOWN_CONFIG:r.update(status='known_config_and_observed_fees',base_rate='0.0025',creator_rate='0.01' if v['creator_fee']>0 else '0',nominal_sum_rate='0.0125' if v['creator_fee']>0 else '0.0025')
 elif e['kind']=='cl':
  ix=tx['transaction']['message']['instructions']+[i for g in tx['meta'].get('innerInstructions') or [] for i in g['instructions']]
  configs=[i['accounts'][1] for i in ix if i.get('programId','').startswith('CAMMC') and len(i.get('accounts',[]))>2 and i['accounts'][2]==pool]
  if configs:
   try:a=rpc('getAccountInfo',[configs[-1],{'encoding':'base64'}],'accounts/'+configs[-1])
   except RuntimeError:
    keys=json.loads((ROOT.parent/'solana_three_check/metadata_keys.json').read_text());vals=json.loads((ROOT.parent/'solana_three_check/metadata_accounts.json').read_text())['result']['value'];a={'value':dict(zip(keys,vals)).get(configs[-1])}
   if a.get('value'):
    raw=base64.b64decode(a['value']['data'][0]);rate=struct.unpack_from('<I',raw,47)[0];r.update(config=configs[-1],nominal_sum_rate=str(D(rate)/10**6),status='current_config_historical_event_check_pending')
  historical=[]
  for l in tx['meta'].get('logMessages') or []:
   if not l.startswith('Program data: '):continue
   d=base64.b64decode(l[14:])
   if d[:8]==SWAP and len(d)==221 and b58(d[8:40])==pool:
    amt0,tf0,amt1,tf1=struct.unpack_from('<4Q',d,136);f0,f1=struct.unpack_from('<2Q',d,205);z=bool(d[168]);historical.append({'amount0':amt0,'amount1':amt1,'transfer0':tf0,'transfer1':tf1,'fee0':f0,'fee1':f1,'zero_for_one':z})
  r['historical_events']=historical
  if historical:
   v=historical[-1];inp=v['amount0'] if v['zero_for_one'] else v['amount1'];fee=v['fee0'] if v['zero_for_one'] else v['fee1'];otherfee=v['fee1'] if v['zero_for_one'] else v['fee0']
   if inp and not otherfee:
    r['observed_rate']=str(D(fee)/inp)
    r['status']='observed_historical_input_fee'
    if 'nominal_sum_rate' in r and abs(D(r['observed_rate'])-D(r['nominal_sum_rate']))<D('0.000001'):r['status']='config_matches_historical_fee'

 elif e['kind']=='dl':
  v=e['event'];fee=v['mm_fee']+v['protocol_fee']+v['limit_order_fee'];den=v['amount_in']-v['amount_left'] if v['fees_on_input'] else v['amount_out']+fee
  r.update(status='observed_dynamic_fee',fees_on_input=v['fees_on_input'],amount_in=v['amount_in'],amount_out=v['amount_out'],fee_raw=fee,observed_rate=str(D(fee)/den) if den else None,reported_fee_bps_raw=v['fee_bps'])
 elif e['kind']=='launch':
  v=e['event'];fee=sum(v[k] for k in ['protocol_fee','platform_fee','creator_fee','share_fee']);den=v['amount_in'] if v['direction']==0 else v['amount_out']+fee
  r.update(status='observed_launch_fee',fee_raw=fee,observed_rate=str(D(fee)/den) if den else None,components={k:v[k] for k in ['protocol_fee','platform_fee','creator_fee','share_fee']})
 out.append(r);(ROOT/'fees.json').write_text(json.dumps(out,indent=2))
(ROOT/'fees.json').write_text(json.dumps(out,indent=2));print('fees',len(out))
