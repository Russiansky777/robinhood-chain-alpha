from pathlib import Path
import zipfile,json
p=Path(__file__).parent;mf=p/'raw_manifest.json';manifest=json.loads(mf.read_text()) if mf.exists() else []
done={name for part in manifest for name in part['files']};files=sorted(f for f in p.glob('block_*.json*') if f.name[6:].split('.')[0].isdigit() and f.suffix!='.tmp' and f.name not in done)
z=None;part=None
for f in files:
 if z is None:
  path=p.parent/f'buyer_100_blocks_{len(manifest)+1:03d}.zip';part={'archive':path.name,'files':[]};z=zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=3)
 z.write(f,f.relative_to(p.parent),compress_type=zipfile.ZIP_STORED if f.suffix=='.gz' else zipfile.ZIP_DEFLATED);part['files'].append(f.name)
 if z.fp.tell()>350*1024**2:
  z.close();manifest.append(part);mf.write_text(json.dumps(manifest,indent=2));print(path.name,len(part['files']),flush=True);z=None
if z:
 z.close();manifest.append(part);mf.write_text(json.dumps(manifest,indent=2));print(path.name,len(part['files']),flush=True)
