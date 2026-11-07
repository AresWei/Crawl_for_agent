"""Explicit project-local text model profiles; default remains the original 4B."""
import json
from pathlib import Path

DEFAULT_MODEL='qwen3-4b'
PROFILES={
    'qwen3-4b':{'id':'qwen3-4b','alias':'research-qwen3-4b','relative_path':'models/qwen3-4b/Qwen3-4B-Q4_K_M.gguf',
               'bytes':2497280256,'sha256':'7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5',
               'revision':'bc640142c66e1fdd12af0bd68f40445458f3869b','manifest':'artifacts/model-download-manifest.json'},
    'qwen3-8b':{'id':'qwen3-8b','alias':'research-qwen3-8b','relative_path':'models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf',
               'bytes':5027783488,'sha256':'d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785',
               'revision':'7c41481f57cb95916b40956ab2f0b139b296d974','manifest':'artifacts/model-qwen8b-download-manifest.json'},
    'qwen3-4b-instruct-2507':{'id':'qwen3-4b-instruct-2507','alias':'research-qwen3-4b-instruct-2507',
               'relative_path':'models/qwen3-4b-instruct-2507/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf',
               'bytes':2497280736,'sha256':'2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e',
               'revision':'ae44f08e1392f39c0e474af10c3ff8355c8b6688','manifest':'artifacts/model-instruct4b-download-manifest.json',
               'repository':'bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF',
               'base_repository':'Qwen/Qwen3-4B-Instruct-2507','base_revision':'cdbee75f17c01a7cc42f958dc650907174af0554',
               'base_license':'Apache-2.0','quantizer_license_metadata':None,'quantizer_is_base_model_author':False},
}

def profile(model_id=DEFAULT_MODEL):
    if model_id not in PROFILES:raise ValueError('Unknown project text model')
    return dict(PROFILES[model_id])

def profile_for_path(root,path):
    actual=Path(path).resolve()
    for item in PROFILES.values():
        if actual==(Path(root)/item['relative_path']).resolve():return dict(item)
    raise ValueError('Unrecognized project text model path')

def state_profile(root,state):
    item=profile_for_path(root,state['model'])
    if state.get('model_id',item['id'])!=item['id'] or state.get('alias',item['alias'])!=item['alias']:
        raise ValueError('Managed model identity disagrees with its weights')
    return item

def active_profile(root):
    state=Path(root)/'runtime/model-server/state.json'
    return state_profile(root,json.loads(state.read_text(encoding='utf-8-sig'))) if state.exists() else profile()

def installed_profile(root,model_id):
    item=profile(model_id);root=Path(root)
    weights=root/item['relative_path']
    manifest=json.loads((root/item['manifest']).read_text(encoding='utf-8-sig'))
    verified=any(a.get('path')==item['relative_path'] and a.get('verified_sha256')==item['sha256'] and a.get('bytes')==item['bytes'] for a in manifest['assets'])
    if not verified or manifest.get('model_revision')!=item['revision'] or weights.stat().st_size!=item['bytes']:
        raise ValueError('Model weights lack the matching verified download manifest')
    return {**item,'path':str(weights.resolve())}
