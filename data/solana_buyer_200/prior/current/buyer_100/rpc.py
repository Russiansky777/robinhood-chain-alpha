import json,pathlib,subprocess,time,gzip,threading,os
ROOT=pathlib.Path(__file__).parent
URL='https://solana-rpc.publicnode.com'
OFFLINE=False
_locks={};_guard=threading.Lock();_rate=threading.Lock();_next=0.0;_stop=threading.Event()
def rpc(method,params,name):
 with _guard:lock=_locks.setdefault(name,threading.Lock())
 with lock:
  f=ROOT/(name+'.json');gz=pathlib.Path(str(f)+'.gz')
  if f.exists() or gz.exists():
   try:
    x=json.loads(gzip.decompress(gz.read_bytes()) if gz.exists() else f.read_text())
    if 'result' in x:return x['result']
   except (ValueError,OSError):pass
  if OFFLINE:raise RuntimeError(f"not_cached:{method}:{name}")
  global _next
  if _stop.is_set():raise RuntimeError('RPC stopped after earlier request error')
  with _rate:
   delay=max(0,_next-time.monotonic())
   if delay:time.sleep(delay)
   _next=time.monotonic()+0.25
  if _stop.is_set():raise RuntimeError('RPC stopped after earlier request error')
  p=subprocess.run(['curl','-sS','--compressed','--max-time','30','-H','Content-Type: application/json','--data',json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}),URL],capture_output=True,text=True)
  try:x=json.loads(p.stdout)
  except ValueError:
   _stop.set();raise RuntimeError(p.stderr+' '+p.stdout[:100])
  if 'error' in x:
   _stop.set();raise RuntimeError(x['error'])
  if method=='getBlock' and params[1].get('transactionDetails')=='full':
   tmp=pathlib.Path(str(gz)+'.tmp');tmp.write_bytes(gzip.compress(p.stdout.encode(),compresslevel=3));os.replace(tmp,gz)
  else:
   tmp=pathlib.Path(str(f)+'.tmp');tmp.write_text(p.stdout);os.replace(tmp,f)
  time.sleep(1.5)
  return x['result']
