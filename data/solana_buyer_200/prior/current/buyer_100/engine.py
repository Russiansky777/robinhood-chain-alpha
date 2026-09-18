"""Historical swap decoding. Prices are marginal pool prices, not realizable fills."""
import sys,json,pathlib,hashlib,base64,struct
from decimal import Decimal as D
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent.parent/'solana_three_check'))
from decode import cp_events,dlmm_events,b58,unb58
ROOT=pathlib.Path(__file__).resolve().parent
USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
SWAP=hashlib.sha256(b'event:SwapEvent').digest()[:8]
CP='CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C';DL='LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'
KNOWN_CONFIG='CRRS5ieQmBrZjWhcj99JuGrT5tyuWDaGAXLXLFjbAtjQ'
DL_DISC={bytes(x['discriminator']) for x in json.loads((ROOT.parent/'solana_three_check/schemas/dlmm.json').read_text())['instructions'] if x['name'] in ['swap','swap2','swap_exact_out','swap_exact_out2','swap_with_price_impact','swap_with_price_impact2']}
def normalize(r):
 if r.get('_normalized'):return r
 msg=r['transaction']['message'];keys=msg['accountKeys']
 if keys and isinstance(keys[0],str):
  loaded=r['meta'].get('loadedAddresses') or {};keys=keys+loaded.get('writable',[])+loaded.get('readonly',[]);msg['accountKeys']=keys
  ix=msg['instructions']+[i for g in r['meta'].get('innerInstructions') or [] for i in g['instructions']]
  for i in ix:
   if 'programIdIndex' in i:
    i['programId']=keys[i['programIdIndex']];i['accounts']=[keys[x] for x in i.get('accounts',[])]
 r['_normalized']=True
 return r

def decode_tx(r):
 normalize(r)
 if r.get('meta',{}).get('err') is not None:return []
 m=r['meta'];keys=r['transaction']['message']['accountKeys'];keys=[k['pubkey'] if isinstance(k,dict) else k for k in keys]
 bal={keys[b['accountIndex']]:b for b in m.get('preTokenBalances',[])+m.get('postTokenBalances',[])}
 decimals={b['mint']:b['uiTokenAmount']['decimals'] for b in bal.values()}
 ix=r['transaction']['message']['instructions']+[i for g in m.get('innerInstructions') or [] for i in g['instructions']]
 configs={};dlpairs={};launchpairs={}
 for i in ix:
  a=i.get('accounts',[])
  if i.get('programId','').startswith('LanMV9') and len(a)>=15:launchpairs[a[4]]=(a[9],a[10],a[2])
  if i.get('programId')==CP and len(a)>=13:configs[a[3]]=a[2]
  if i.get('programId')==DL and len(a)>=13 and unb58(i.get('data',''))[:8] in DL_DISC:dlpairs[a[0]]=(a[6],a[7])
 out=[]
 for e in cp_events(r):
  a,b=e['input_mint'],e['output_mint'];p=None;status='unknown_config'
  if configs.get(e['pool'])==KNOWN_CONFIG and a in decimals and b in decimals:
   rin=e['input_vault_before']+e['input_amount']-e['trade_fee']*120000//1000000-e['trade_fee']*40000//1000000-(e['creator_fee'] if e['creator_fee_on_input'] else 0)
   rout=e['output_vault_before']-e['output_amount']-(e['creator_fee'] if not e['creator_fee_on_input'] else e['creator_fee'])
   if rin>0 and rout>0:p=str(D(rout)/D(rin)*D(10)**(decimals[a]-decimals[b]));status='ok'
  out.append(dict(kind='cp',pool=e['pool'],m0=a,m1=b,d0=decimals.get(a),d1=decimals.get(b),p1_per_0=p,status=status,config=configs.get(e['pool']),event=e))
 for l in m.get('logMessages') or []:
  if not l.startswith('Program data: '):continue
  d=base64.b64decode(l[14:])
  if d[:8]!=SWAP or not 205<=len(d)<=221:continue
  a=bal.get(b58(d[72:104]));b=bal.get(b58(d[104:136]));sqrt=int.from_bytes(d[169:185],'little');p=None
  e=dict(kind='cl',pool=b58(d[8:40]),sqrt=str(sqrt),status='missing_mint_metadata')
  if a and b:
   da,db=a['uiTokenAmount']['decimals'],b['uiTokenAmount']['decimals'];p=(D(sqrt)/D(2**64))**2*D(10)**(da-db)
   e.update(m0=a['mint'],m1=b['mint'],d0=da,d1=db,p1_per_0=str(p),status='ok')
  out.append(e)
 for e in dlmm_events(r):
  if e.get('legacy'):continue
  pair=dlpairs.get(e['pool']);v=dict(kind='dl',pool=e['pool'],event=e,status='need_bin_step')
  if pair:v.update(m0=pair[0],m1=pair[1],d0=decimals.get(pair[0]),d1=decimals.get(pair[1]))
  out.append(v)
 for i in ix:
  if not i.get('programId','').startswith('LanMV9') or 'data' not in i:continue
  raw=unb58(i['data'])
  if raw[8:16]!=bytes([189,219,127,211,78,230,97,238]) or len(raw)<155:continue
  d=raw[16:];pool=b58(d[:32]);pair=launchpairs.get(pool)
  if not pair:continue
  names=['total_base_sell','virtual_base','virtual_quote','real_base_before','real_quote_before','real_base_after','real_quote_after','amount_in','amount_out','protocol_fee','platform_fee','creator_fee','share_fee']
  e=dict(zip(names,struct.unpack_from('<13Q',d,32)));e.update(direction=d[136],pool_status=d[137],exact_in=bool(d[138]))
  a,b,c=pair;out.append(dict(kind='launch',pool=pool,m0=a,m1=b,d0=decimals.get(a),d1=decimals.get(b),config=c,event=e,status='need_curve_config'))
 return out
if __name__=='__main__':
 rows=json.loads((ROOT/'selected.json').read_text());inventory=[]
 for r in rows:
  tx=json.loads((ROOT/'tx'/f"{r['signature']}.json").read_text())['result'];ev=decode_tx(tx)
  inventory.append(dict(signature=r['signature'],mint=r['mint'],events=ev,target_pools=sorted({e['pool'] for e in ev if r['mint'] in [e.get('m0'),e.get('m1')]})))
 (ROOT/'pool_inventory.json').write_text(json.dumps(inventory,indent=2))
 print('purchases',len(rows),'target pool decoded',sum(bool(x['target_pools']) for x in inventory),'pools',len({e['pool'] for x in inventory for e in x['events']}))

# Detect actual pool swaps whose price events are missing (e.g. truncated logs).
def expected_swaps(r,pool):
 normalize(r)
 counts=0
 if r['meta']['err']:return 0
 ix=r['transaction']['message']['instructions']+[i for g in r['meta'].get('innerInstructions') or [] for i in g['instructions']]
 cp_discs={hashlib.sha256(('global:'+s).encode()).digest()[:8] for s in ['swap_base_input','swap_base_output']}
 cl_discs={hashlib.sha256(('global:'+s).encode()).digest()[:8] for s in ['swap','swap_v2']}
 launch_discs={hashlib.sha256(('global:'+s).encode()).digest()[:8] for s in ['buy_exact_in','buy_exact_out','sell_exact_in','sell_exact_out']}
 for i in ix:
  a=i.get('accounts',[]);pr=i.get('programId','')
  if pr not in [CP,DL] and not pr.startswith(('CAMMC','LanMV9')):continue
  raw=unb58(i.get('data',''))[:8]
  if pr==CP and len(a)>3 and a[3]==pool and raw in cp_discs:counts+=1
  elif pr.startswith('CAMMC') and len(a)>2 and a[2]==pool and raw in cl_discs:counts+=1
  elif pr==DL and a and a[0]==pool and raw in DL_DISC:counts+=1
  elif pr.startswith('LanMV9') and len(a)>4 and a[4]==pool and raw in launch_discs:counts+=1
 return counts

def expected_pool_counts(r):
 normalize(r)
 if r['meta']['err']:return {}
 counts={};ix=r['transaction']['message']['instructions']+[i for g in r['meta'].get('innerInstructions') or [] for i in g['instructions']]
 for i in ix:
  a=i.get('accounts',[]);pr=i.get('programId','');pool=None
  if pr not in [CP,DL] and not pr.startswith(('CAMMC','LanMV9')):continue
  raw=unb58(i.get('data',''))[:8]
  if pr==CP and len(a)>3 and raw in {hashlib.sha256(('global:'+s).encode()).digest()[:8] for s in ['swap_base_input','swap_base_output']}:pool=a[3]
  elif pr.startswith('CAMMC') and len(a)>2 and raw in {hashlib.sha256(('global:'+s).encode()).digest()[:8] for s in ['swap','swap_v2']}:pool=a[2]
  elif pr==DL and a and raw in DL_DISC:pool=a[0]
  elif pr.startswith('LanMV9') and len(a)>4 and raw in {hashlib.sha256(('global:'+s).encode()).digest()[:8] for s in ['buy_exact_in','buy_exact_out','sell_exact_in','sell_exact_out']}:pool=a[4]
  if pool:counts[pool]=counts.get(pool,0)+1
 return counts
