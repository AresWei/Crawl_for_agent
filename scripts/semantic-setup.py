"""Install only pinned public semantic assets; packages are installed by .ps1."""
from pathlib import Path
import hashlib
import json
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
REV = 'e8f8c211226b894fcb81acc59f3b34ba3efd5f42'
FILES = ['onnx/model_quint8_avx2.onnx', 'tokenizer.json', 'tokenizer_config.json',
         'config.json', 'sentence_bert_config.json', '1_Pooling/config.json', 'README.md']

def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()

def main():
    folder = ROOT / 'models/semantic/multilingual-minilm'
    folder.mkdir(parents=True, exist_ok=True)
    meta = json.loads(fetch(f'https://huggingface.co/api/models/{REPO}/revision/{REV}?blobs=true'))
    (folder / 'publisher-metadata.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    entries = []
    for name in FILES:
        item = next(x for x in meta['siblings'] if x['rfilename'] == name)
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = item.get('lfs', {}).get('sha256')
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if not digest or (expected and digest != expected):
            data = fetch(f'https://huggingface.co/{REPO}/resolve/{REV}/{name}?download=true')
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != item['size'] or (expected and digest != expected):
                raise ValueError('Publisher checksum/size mismatch: '+name)
            path.write_bytes(data)
        entries.append({'path':path.relative_to(ROOT).as_posix(), 'sha256':digest, 'bytes':path.stat().st_size})
        print('Verified', name, path.stat().st_size, flush=True)
    license_path = folder / 'LICENSE'
    license_path.write_bytes(fetch('https://www.apache.org/licenses/LICENSE-2.0.txt'))
    nltk_root = ROOT / 'cache/nltk'
    nltk_root.mkdir(parents=True, exist_ok=True)
    nltk_files = []
    for package, category, expected in [
        ('punkt_tab','tokenizers','e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106'),
        ('stopwords','corpora','48c0e52d8b52546e827f53761fb30300c0ab94f70660d28bd65ba0a86270946b')]:
        path = nltk_root / category / (package+'.zip')
        path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            data = fetch(f'https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/{category}/{package}.zip')
            if hashlib.sha256(data).hexdigest()!=expected:
                raise ValueError('Pinned NLTK data checksum mismatch')
            path.write_bytes(data)
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if not (path.parent/member.filename).resolve().is_relative_to(path.parent.resolve()):
                    raise ValueError('NLTK archive path outside data root')
            archive.extractall(path.parent)
        nltk_files.append({'path':path.relative_to(ROOT).as_posix(), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes':path.stat().st_size})
    manifest = {'model':REPO,'revision':REV,'license':'Apache-2.0','backend':'onnxruntime CPUExecutionProvider',
        'pooling':'attention_mask_mean_then_l2','max_tokens':128,'dimensions':384,
        'implementation_version':'onnx-masked-mean-l2-v1','files':entries,'nltk_data':nltk_files,
        'topic_classifier':{'model':'cardiffnlp/tweet-topic-21-multi','status':'optional_not_installed','license':'MIT','language':'en'}}
    (ROOT/'runtime/semantic-model-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()
