"""Pinned, public SD1.5 and native CUDA backend setup; no GPU inference."""
from pathlib import Path
import concurrent.futures, hashlib, json, urllib.request, zipfile, time
ROOT = Path(__file__).resolve().parents[1]
TAG = 'master-1052-f5df5f8'
REV = '451f4fe16113bff5a5d2269ed5ad43b0592e9a14'
ASSETS = [
 ('sd-master-f5df5f8-bin-win-cuda12-x64.zip','607b0336e245f5731d7693fa0ff79f1a98f8cc5a45b9678a41407788b23e2fea'),
 ('cudart-sd-bin-win-cu12-x64.zip','fe20366827d357c00797eebb58244dddab7fd9a348d70090c3871004c320f38d')]
def digest(path):
 with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()
def fetch(relative,url,expected=None):
 path=ROOT/relative; path.parent.mkdir(parents=True,exist_ok=True)
 if not path.exists() or (expected and digest(path)!=expected):
  part=path.with_suffix(path.suffix+'.part')
  if relative.endswith('.safetensors'):
   size=4265146304; chunk_size=16*1024*1024
   chunks=ROOT/'temp/creation-download'; chunks.mkdir(parents=True,exist_ok=True)
   def segment(start):
    end=min(start+chunk_size,size)-1; target=chunks/f'{start}.part'
    if target.exists() and target.stat().st_size==end-start+1: return target
    for attempt in range(3):
     try:
      request=urllib.request.Request(url,headers={'Range':f'bytes={start}-{end}'})
      with urllib.request.urlopen(request,timeout=60) as response:
       if response.status!=206 or not response.headers.get('Content-Range','').startswith(f'bytes {start}-{end}/'): raise ValueError('Incorrect range response')
       data=response.read(end-start+2)
       if len(data)!=end-start+1: raise ValueError('Incomplete range')
      target.write_bytes(data); return target
     except Exception:
      if attempt==2: raise
      time.sleep(1)
   with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: pieces=list(pool.map(segment,range(0,size,chunk_size)))
   with part.open('wb') as out:
    for piece in pieces: out.write(piece.read_bytes())
  else:
   with urllib.request.urlopen(url,timeout=120) as response,part.open('wb') as out:
    while chunk:=response.read(4*1024*1024): out.write(chunk)
  if expected and digest(part)!=expected: raise ValueError('Publisher hash mismatch: '+relative)
  part.replace(path)
 return {'path':relative,'sha256':digest(path),'bytes':path.stat().st_size,'url':url,'publisher_sha256':expected}
def main():
 tasks=[('runtime/creation/downloads/'+name,f'https://github.com/leejet/stable-diffusion.cpp/releases/download/{TAG}/{name}',h) for name,h in ASSETS]
 tasks += [('models/creation/v1-5-pruned-emaonly.safetensors',f'https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/{REV}/v1-5-pruned-emaonly.safetensors','6ce0161689b3853acaa03779ec93eafe75a02f4ced659bee03f50797806fa2fa'),('models/creation/README.model.md',f'https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/{REV}/README.md',None),('runtime/creation/LICENSE.backend',f'https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/{TAG}/LICENSE',None),('models/creation/LICENSE.model','https://raw.githubusercontent.com/CompVis/stable-diffusion/main/LICENSE',None)]
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool: files=list(pool.map(lambda args:fetch(*args),tasks))
 dest=ROOT/'runtime/creation/bin'; dest.mkdir(parents=True,exist_ok=True)
 for name,_ in ASSETS:
  with zipfile.ZipFile(ROOT/'runtime/creation/downloads'/name) as z:
   for item in z.infolist():
    if item.is_dir(): continue
    target=dest/Path(item.filename).name
    target.write_bytes(z.read(item))
 binaries=[{'path':p.relative_to(ROOT).as_posix(),'sha256':digest(p),'bytes':p.stat().st_size} for p in dest.iterdir() if p.is_file()]
 manifest={'schema_version':1,'backend':{'name':'stable-diffusion.cpp','revision':TAG,'license':'MIT','executable':'runtime/creation/bin/sd-cli.exe'},'model':{'name':'stable-diffusion-v1-5/stable-diffusion-v1-5','revision':REV,'license':'CreativeML OpenRAIL-M','path':tasks[2][0],'sha256':tasks[2][2]},'files':files,'binaries':binaries}
 (ROOT/'runtime/creation-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8'); print(json.dumps({'ok':True,'files':len(files),'binaries':len(binaries)}))
if __name__=='__main__': main()
