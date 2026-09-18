import base64,hashlib,struct,json,pathlib
A='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
def b58(b):
 n=int.from_bytes(b,'big');s=''
 while n:n,r=divmod(n,58);s=A[r]+s
 return '1'*(len(b)-len(b.lstrip(b'\0')))+s
def cp_events(r):
 out=[];program=[]
 for l in r['meta'].get('logMessages',[]):
  if l.startswith('Program data: '):
   d=base64.b64decode(l[14:])
   if d[:8]!=hashlib.sha256(b'event:SwapEvent').digest()[:8] or len(d)<153:continue
   pool=b58(d[8:40])
   if len(d)!=170:continue
   v=struct.unpack_from('<6Q',d,40)
   e=dict(pool=pool,**dict(zip(['input_vault_before','output_vault_before','input_amount','output_amount','input_transfer_fee','output_transfer_fee'],v)),base_input=bool(d[88]),input_mint=b58(d[89:121]),output_mint=b58(d[121:153]))
   if len(d)>=170:e.update(trade_fee=struct.unpack_from('<Q',d,153)[0],creator_fee=struct.unpack_from('<Q',d,161)[0],creator_fee_on_input=bool(d[169]))
   out.append(e)
 return out
if __name__=='__main__':
 r=json.load(open('solana_wow_check/tx_3.json'))['result'];print(json.dumps(cp_events(r),indent=2))

def unb58(s):
 n=0
 for c in s:n=n*58+A.index(c)
 return b'\0'*(len(s)-len(s.lstrip('1')))+n.to_bytes((n.bit_length()+7)//8,'big')
def dlmm_events(r):
 out=[]
 for group in r['meta'].get('innerInstructions',[]):
  for ix in group['instructions']:
   if ix.get('programId')!='LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo' or 'data' not in ix:continue
   d=unb58(ix['data'])
   if d[8:16]==bytes([81,108,227,190,205,208,10,196]):
    raw=d[16:];e={'pool':b58(raw[:32]),'from':b58(raw[32:64]),'legacy':True};out.append(e);continue
   if d[8:16]!=bytes([46,116,82,215,148,27,84,77]):continue
   d=d[16:];i=0;e={}
   for n in ['pool','from']:e[n]=b58(d[i:i+32]);i+=32
   for n in ['start_bin','end_bin']:e[n]=struct.unpack_from('<i',d,i)[0];i+=4
   e['swap_for_y']=bool(d[i]);i+=1;e['fee_bps']=int.from_bytes(d[i:i+16],'little');i+=16
   for n in ['amount_in','amount_left','amount_out','mm_fee','protocol_fee','limit_order_fee','host_fee']:e[n]=struct.unpack_from('<Q',d,i)[0];i+=8
   e['fees_on_input']=bool(d[i]);e['fees_on_token_x']=bool(d[i+1]);out.append(e)
 return out
