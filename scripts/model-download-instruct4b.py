"""Pinned non-thinking 4B candidate; third-party quantization is explicit."""
import hashlib,importlib.util,json,subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
REPO='bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF'
REV='ae44f08e1392f39c0e474af10c3ff8355c8b6688'
BASE='Qwen/Qwen3-4B-Instruct-2507'
BASE_REV='cdbee75f17c01a7cc42f958dc650907174af0554'
NAME='Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf'
SHA='2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e'
SIZE=2497280736


def main():
    source=json.loads((ROOT/'artifacts/model-instruct4b-source.json').read_text(encoding='utf-8-sig'))
    base=json.loads((ROOT/'artifacts/model-instruct4b-base-source.json').read_text(encoding='utf-8-sig'))
    row=next(row for row in source['siblings'] if row['rfilename']==NAME)
    assert source['id']==REPO and source['sha']==REV and row['size']==SIZE and row['lfs']['sha256']==SHA
    assert base['id']==BASE and base['sha']==BASE_REV and base['cardData']['license']=='apache-2.0'
    folder=ROOT/'models/qwen3-4b-instruct-2507';folder.mkdir(parents=True,exist_ok=True)
    notices=[]
    for name,url in [('README.quantizer.md',f'https://huggingface.co/{REPO}/resolve/{REV}/README.md'),
                     ('README.base.md',f'https://huggingface.co/{BASE}/resolve/{BASE_REV}/README.md'),
                     ('LICENSE.base',f'https://huggingface.co/{BASE}/resolve/{BASE_REV}/LICENSE')]:
        dest=folder/name
        subprocess.run(['curl.exe','--fail','--location','--retry','2','--connect-timeout','20','--max-time','60',
                        '--silent','--show-error','--output',str(dest),url],check=True)
        notices.append({'path':str(dest.relative_to(ROOT)),'url':url,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
    spec=importlib.util.spec_from_file_location('project_downloader',ROOT/'scripts/model-download.py')
    downloader=importlib.util.module_from_spec(spec);spec.loader.exec_module(downloader)
    asset={'name':NAME,'path':f'models/qwen3-4b-instruct-2507/{NAME}',
           'url':f'https://huggingface.co/{REPO}/resolve/{REV}/{NAME}?download=true',
           'bytes':SIZE,'sha256':SHA,'assembly_buffer_bytes':32*1024*1024}
    verified=downloader.download(asset)
    manifest={'model_id':'qwen3-4b-instruct-2507','alias':'research-qwen3-4b-instruct-2507',
              'repository':REPO,'model_revision':REV,'base_repository':BASE,'base_revision':BASE_REV,
              'license':'Apache-2.0 (base model)','quantizer_license_metadata':source.get('cardData',{}).get('license'),
              'quantizer_is_base_model_author':False,'runtime_reused':True,'llama_release':'b12170',
              'assets':[verified],'notices':notices,'default_changed':False}
    (ROOT/'artifacts/model-instruct4b-download-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Pinned 4B instruction candidate verified; default and running worker unchanged',flush=True)

if __name__=='__main__':main()
