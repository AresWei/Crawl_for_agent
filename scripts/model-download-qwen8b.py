"""Pinned official optional 8B weights, using the existing range/hash downloader."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
REVISION='7c41481f57cb95916b40956ab2f0b139b296d974'
REPO='Qwen/Qwen3-8B-GGUF'
NAME='Qwen3-8B-Q4_K_M.gguf'
SHA256='d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785'
SIZE=5027783488

def fetch(url,path):
    subprocess.run(['curl.exe','--fail','--location','--retry','2','--connect-timeout','30','--max-time','90',
                    '--silent','--show-error','--output',str(path),url],check=True)

def main():
    folder=ROOT/'models/qwen3-8b';folder.mkdir(parents=True,exist_ok=True)
    api=ROOT/'artifacts/model-qwen8b-source.json'
    reuse='--reuse-verified-chunks' in sys.argv
    if not reuse:fetch(f'https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true',api)
    source=json.loads(api.read_text(encoding='utf-8'))
    assert source['id']==REPO and source['sha']==REVISION
    assert source['cardData']['license']=='apache-2.0'
    files={row['rfilename']:row for row in source['siblings']}
    assert files[NAME]['lfs']['sha256']==SHA256 and files[NAME]['size']==SIZE
    notices=[]
    for name in ['README.md','LICENSE']:
        path=folder/name
        if not reuse:fetch(f'https://huggingface.co/{REPO}/resolve/{REVISION}/{name}',path)
        raw=path.read_bytes();git_hash=hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        assert git_hash==files[name]['blobId'] and len(raw)==files[name]['size']
        notices.append({'path':str(path.relative_to(ROOT)),'git_blob_sha1':git_hash,'sha256':hashlib.sha256(raw).hexdigest()})
    spec=importlib.util.spec_from_file_location('existing_model_download',ROOT/'scripts/model-download.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if reuse:
        def assemble_existing(asset,partial):
            size=32*1024*1024
            chunks=partial.parent/(asset['name']+'.chunks')
            count=(asset['bytes']+size-1)//size
            paths=[chunks/f'{i:04d}.bin' for i in range(count)]
            assert all(p.stat().st_size==min(size,asset['bytes']-i*size) for i,p in enumerate(paths)), 'All completed ranges required; no network fallback'
            # Keep complete already-copied ranges; final whole-file SHA remains
            # authoritative. Discard only the interrupted partial last range.
            copied=min(partial.stat().st_size//size,count-1) if partial.exists() else 0
            started=time.monotonic()
            with partial.open('r+b' if partial.exists() else 'wb') as out:
                out.truncate(copied*size);out.seek(copied*size)
                for path in paths[copied:]:
                    out.write(path.read_bytes())
            print(json.dumps({'assembly_buffer_bytes':size,'reused_complete_ranges':copied,'remaining_ranges':count-copied,
                              'assembly_seconds':round(time.monotonic()-started,3)}),flush=True)
        module.segmented_download=assemble_existing
    asset={'name':NAME,'path':f'models/qwen3-8b/{NAME}',
           'url':f'https://huggingface.co/{REPO}/resolve/{REVISION}/{NAME}?download=true','bytes':SIZE,'sha256':SHA256}
    verified=module.download(asset)
    manifest={'model_id':'qwen3-8b','alias':'research-qwen3-8b','repository':REPO,'model_revision':REVISION,
              'license':'Apache-2.0','llama_release':'b12170','runtime_reused':True,'assets':[verified],
              'notices':notices,'default_changed':False}
    (ROOT/'artifacts/model-qwen8b-download-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print('Optional 8B download verified; no worker started or default changed',flush=True)

if __name__=='__main__':main()
