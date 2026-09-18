from rpc import *
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
W='Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit';U='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';SOL='So11111111111111111111111111111111111111112';(ROOT/'tx').mkdir(exist_ok=True)
rows=[];checks=[];errors=[]
def classify(r,h):
 m=r['meta'];keys=r['transaction']['message']['accountKeys'];sig=r['transaction']['signatures'][0]
 signed=any(k.get('signer') and k['pubkey']==W for k in keys)
 def bals(tag):
  a={}
  for b in m[tag]:
   if b.get('owner')==W:a[b['mint']]=a.get(b['mint'],D(0))+D(b['uiTokenAmount']['amount'])/D(10)**b['uiTokenAmount']['decimals']
  return a
 pre,post=bals('preTokenBalances'),bals('postTokenBalances');delta={k:post.get(k,D(0))-pre.get(k,D(0)) for k in set(pre)|set(post)};positive=[k for k,v in delta.items() if v>0 and k not in [U,SOL]];spent=-delta.get(U,D(0));allix=r['transaction']['message']['instructions']+[ix for g in m.get('innerInstructions') or [] for ix in g['instructions']];programs=sorted({x.get('programId','') for x in allix})
 check={'signature':sig,'slot':r['slot'],'time':r['blockTime'],'wallet_signed':signed,'usdc_spent':str(spent),'positive_mints':positive,'status':'not_selected'}
 if not signed:check['status']='wallet_not_signer'
 elif len(positive)==1 and spent>500 and any('Instruction: Swap' in x or 'Instruction: Buy' in x for x in m.get('logMessages') or []):
  mint=positive[0];row={'signature':sig,'slot':r['slot'],'transaction_index':h.get('transactionIndex',r.get('transactionIndex')),'time':r['blockTime'],'mint':mint,'usdc_spent':str(spent),'tokens_received':str(delta[mint]),'balance_before':str(pre.get(mint,D(0))),'zero_balance':pre.get(mint,D(0))==0,'entry_usdc':str(spent/delta[mint]),'programs':programs,'network_fee_sol':str(D(m['fee'])/10**9)};rows.append(row);check['status']='selected'
 elif positive and spent>500:check['status']='ambiguous_buy'
 elif positive and any(v<0 for k,v in delta.items() if k!=U) and spent<=0:check['status']='non_usdc_exchange_needs_valuation'
 checks.append(check)
def fetch(h):
 try:
  r=rpc('getTransaction',[h['signature'],{'encoding':'jsonParsed','maxSupportedTransactionVersion':1}],'tx/'+h['signature'])
  if r is None:raise RuntimeError('null transaction; selection cannot skip this record')
  return h,r
 except Exception as err:raise RuntimeError(h['signature']+': '+str(err)) from err
def save():
 (ROOT/'selected.json').write_text(json.dumps(rows[:100],indent=2));(ROOT/'selection_audit.json').write_text(json.dumps(checks,indent=2));(ROOT/'selection_errors.json').write_text(json.dumps(errors,indent=2))
with ThreadPoolExecutor(max_workers=8) as ex:
 for page in range(5):
  opts={'limit':1000}
  if page:opts['before']=history[-1]['signature']
  history=rpc('getSignaturesForAddress',[W,opts],f'wallet_history_{page}')
  eligible=[h for h in history if h['err'] is None]
  for i in range(0,len(eligible),8):
   futures=[ex.submit(fetch,h) for h in eligible[i:i+8]]
   for fu in futures:
    try:
     h,r=fu.result()
     if r:classify(r,h)
     else:errors.append({'signature':h['signature'],'error':'null transaction'})
    except Exception as err:errors.append({'error':str(err)});save();raise
   save();print('checked',len(checks),'selected',len(rows),'zero',sum(x['zero_balance'] for x in rows),flush=True)
   if len(rows)>=100:break
  if len(rows)>=100 or len(history)<1000:break
save()
