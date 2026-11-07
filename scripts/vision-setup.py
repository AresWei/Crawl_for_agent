"""Pin public upstream model metadata, licenses and SHA256-verified GGUF assets."""
from pathlib import Path
import hashlib
import json
import urllib.request
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
REPO = 'ggml-org/SmolVLM2-2.2B-Instruct-GGUF'
FILES = ['SmolVLM2-2.2B-Instruct-Q4_K_M.gguf', 'mmproj-SmolVLM2-2.2B-Instruct-Q8_0.gguf']

def get(url):
    with urllib.request.urlopen(url, timeout=90) as response:
        return response.read()

def main():
    folder = ROOT / 'models/vision/smolvlm2-2.2b'
    folder.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(get('https://huggingface.co/api/models/' + REPO))
    original = json.loads(get('https://huggingface.co/api/models/HuggingFaceTB/SmolVLM2-2.2B-Instruct'))
    if metadata.get('gated') or original.get('gated'): raise ValueError('Only ungated public weights allowed')
    revision = metadata['sha']
    listing = json.loads(get(f'https://huggingface.co/api/models/{REPO}/tree/{revision}'))
    (folder / 'publisher-metadata.json').write_text(json.dumps({'model':metadata,'original':original,'files':listing}, indent=2), encoding='utf-8')
    sources = {
        'README.gguf.md': f'https://huggingface.co/{REPO}/raw/{revision}/README.md',
        'README.original.md':f'https://huggingface.co/HuggingFaceTB/SmolVLM2-2.2B-Instruct/raw/{original["sha"]}/README.md',
        'LICENSE':'https://www.apache.org/licenses/LICENSE-2.0.txt',
        'llama-multimodal.md':'https://raw.githubusercontent.com/ggml-org/llama.cpp/b12170/docs/multimodal.md',
    }
    documents = []
    for name,url in sources.items():
        data = get(url)
        (folder / name).write_bytes(data)
        documents.append({'path':str((folder/name).relative_to(ROOT)).replace('\\','/'),'source_url':url,
            'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
    entries = []
    for name in FILES:
        item = next(item for item in listing if item['path'] == name)
        expected = item['lfs']['oid']
        destination = folder / name
        actual = None
        if destination.exists():
            with destination.open('rb') as source: actual = hashlib.file_digest(source,'sha256').hexdigest()
        if actual != expected:
            temporary = destination.with_suffix('.part')
            url = f'https://huggingface.co/{REPO}/resolve/{revision}/{name}?download=true'
            request = urllib.request.Request(url)
            offset = temporary.stat().st_size if temporary.exists() else 0
            if offset: request.add_header('Range', f'bytes={offset}-')
            with urllib.request.urlopen(request, timeout=120) as response:
                mode = 'ab' if offset and response.status == 206 else 'wb'
                with temporary.open(mode) as output:
                    while chunk := response.read(1024*1024): output.write(chunk)
            with temporary.open('rb') as source: actual = hashlib.file_digest(source, 'sha256').hexdigest()
            if actual != expected or temporary.stat().st_size != item['size']: raise ValueError('Publisher hash/size mismatch: '+name)
            temporary.replace(destination)
        entries.append({'path':str(destination.relative_to(ROOT)).replace('\\','/'),'sha256':expected,'bytes':item['size']})
        print('Verified', name, item['size'], flush=True)
    manifest = {'schema_version':1,'model':REPO,'revision':revision,'license':'Apache-2.0','backend':'llama.cpp b12170',
                'weights':entries[0],'projector':entries[1],'sources':sources,'files':entries,'documents':documents,
                'fetched_at':datetime.now(timezone.utc).isoformat(),'original_revision':original['sha']}
    (ROOT / 'runtime/vision-model-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

if __name__ == '__main__': main()
