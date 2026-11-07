"""Local model assets and private, explicitly selected model API profiles."""
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


def validate_api_url(value):
    """An explicit endpoint, never URL credentials or environment routing."""
    import ipaddress
    from urllib.parse import urlsplit
    if not isinstance(value, str) or not value or len(value)>2048 or any(c.isspace() or ord(c)<32 for c in value) or '\\' in value:
        raise ValueError('Invalid model API base URL')
    try:
        parsed=urlsplit(value)
        port=parsed.port
        loopback=parsed.hostname=='localhost'
        try:loopback=loopback or ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:pass
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or '?' in value or '#' in value
                or parsed.scheme not in {'http','https'} or (parsed.scheme=='http' and not loopback)):
            raise ValueError()
    except ValueError:
        raise ValueError('Model API URL requires HTTPS or loopback HTTP without credentials, query or fragment') from None
    return value.rstrip('/')


class ModelProfileStore:
    """Private API settings: Windows DPAPI, owner-only storage on other systems."""
    def __init__(self,root):
        from .paths import AppPaths
        self.directory=Path(root.root if isinstance(root,AppPaths) else root).resolve()/'state'/'model-api'

    def _path(self,name):
        from .paths import safe_name
        if not isinstance(name,str):raise ValueError('Invalid model API profile name')
        return self.directory/(safe_name(name)+'.json')

    @staticmethod
    def _safe(value):
        return {**{key:value[key] for key in ('name','provider','base_url','model','revision')},'configured':True}

    def save(self,name,*,provider,base_url,model,api_key):
        import uuid
        from .accounts import write_private
        path=self._path(name)
        if provider not in {'openai','anthropic'}:raise ValueError('Unsupported model API provider')
        base_url=validate_api_url(base_url)
        if not isinstance(model,str) or not model.strip() or len(model)>200 or any(ord(c)<32 for c in model):
            raise ValueError('Invalid model API model identifier')
        if not isinstance(api_key,str) or not api_key.strip() or len(api_key)>8192 or any(ord(c)<33 or ord(c)>126 for c in api_key):
            raise ValueError('Invalid model API credential')
        value={'name':name,'provider':provider,'base_url':base_url,'model':model.strip(),
               'api_key':api_key,'revision':uuid.uuid4().hex}
        write_private(path,value)
        return self._safe(value)

    def load(self,name):
        from .accounts import read_private
        value=read_private(self._path(name))
        if value.get('name')!=name or value.get('provider') not in {'openai','anthropic'}:
            raise ValueError('Invalid model API profile')
        validate_api_url(value['base_url'])
        return value

    def list(self):
        return [self._safe(self.load(path.stem)) for path in sorted(self.directory.glob('*.json'))]

    def delete(self,name):
        self._path(name).unlink(missing_ok=True)
