from pathlib import Path
import zipfile,json
p=Path(__file__).parent
with zipfile.ZipFile(p.parent/'buyer_100_checkpoint.zip','w',zipfile.ZIP_DEFLATED,compresslevel=5) as z:
 for f in p.rglob('*'):
  if not f.is_file() or '__pycache__' in str(f) or f.suffix=='.tmp':continue
  if f.name.startswith('block_') and f.name[6:].split('.')[0].isdigit():continue
  z.write(f,f.relative_to(p.parent))
 for folder,files in {'solana_three_check':['decode.py','pool_meta.json','entries.json','results.json','quote_checks.json','metadata_keys.json','metadata_accounts.json','schemas/dlmm.json','schemas/states_config.rs','schemas/states_events.rs','schemas/clmm_events.rs'],'solana_wow_check':['price_results.json','block_points_refined.json']}.items():
  for rel in files:
   f=p.parent/folder/rel;z.write(f,f.relative_to(p.parent))
with zipfile.ZipFile(p.parent/'buyer_100_checkpoint.zip') as z:
 assert len(json.loads(z.read('buyer_100/selected.json')))==100
 assert len(json.loads(z.read('buyer_100/price_points.json')))==1000
 assert json.loads(z.read('buyer_100/checkpoint.json'))['status']=='blocked_network_access'
print('checkpoint MB',round((p.parent/'buyer_100_checkpoint.zip').stat().st_size/1e6,2))
