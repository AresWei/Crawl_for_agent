"""Pinned original Crawl4AI multi-label model, with public publisher evidence."""
from pathlib import Path
import hashlib
import json
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPO = 'cardiffnlp/tweet-topic-21-multi'
REV = '8b1867b06d8756ccc132f58b5a99666c22d1a4e7'
FILES = ['pytorch_model.bin','config.json','tokenizer.json','tokenizer_config.json',
         'special_tokens_map.json','vocab.json','merges.txt','README.md']

def get(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()

def main():
    folder = ROOT/'models/semantic/tweet-topic-21-multi'
    folder.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(get(f'https://huggingface.co/api/models/{REPO}/revision/{REV}?blobs=true'))
    (folder/'publisher-metadata.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    entries = []
    for name in FILES:
        item = next(x for x in metadata['siblings'] if x['rfilename']==name)
        expected = item.get('lfs',{}).get('sha256')
        path = folder/name
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if not digest or (expected and digest!=expected):
            partial = path.with_suffix(path.suffix+'.part')
            with urllib.request.urlopen(f'https://huggingface.co/{REPO}/resolve/{REV}/{name}?download=true',timeout=120) as response, partial.open('wb') as out:
                while chunk := response.read(1024*1024):
                    out.write(chunk)
            with partial.open('rb') as source:
                digest = hashlib.file_digest(source,'sha256').hexdigest()
            if partial.stat().st_size!=item['size'] or (expected and digest!=expected):
                raise ValueError('Publisher hash/size mismatch: '+name)
            partial.replace(path)
        entries.append({'path':path.relative_to(ROOT).as_posix(),'bytes':path.stat().st_size,'sha256':digest})
        print('Verified',name,path.stat().st_size,flush=True)
    # Model card explicitly declares MIT; preserve that declaration verbatim.
    configuration = json.loads((folder/'config.json').read_text(encoding='utf-8'))
    manifest = {'model':REPO,'revision':REV,'license':'MIT','license_evidence':'README.md YAML license: mit',
        'language':'en','backend':'torch CPU','max_tokens':64,'threshold':0.5,'files':entries,
        'label_count':len(configuration['id2label']),'id2label':configuration['id2label']}
    (ROOT/'runtime/semantic-topic-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    embedding_path = ROOT/'runtime/semantic-model-manifest.json'
    if embedding_path.exists():
        embedding = json.loads(embedding_path.read_text(encoding='utf-8'))
        embedding['topic_classifier'] = {key:manifest[key] for key in ['model','revision','license','language','label_count']}
        embedding['topic_classifier'].update(status='installed',manifest_path='runtime/semantic-topic-manifest.json')
        embedding_path.write_text(json.dumps(embedding,indent=2),encoding='utf-8')

if __name__=='__main__':
    main()
