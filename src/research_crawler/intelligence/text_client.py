"""Explicit account, named API, or local execution; no automatic API fallback."""
from .local_model import LocalModelClient


def text_client(root, backend='local', *, api_profile=None):
    if backend!='api' and api_profile is not None:raise ValueError('api_profile requires answer_backend api')
    if backend == 'local':return LocalModelClient(root)
    if backend == 'codex':
        from .codex_model import CodexModelClient
        return CodexModelClient(root)
    if backend == 'api':
        if not isinstance(api_profile,str) or not api_profile:raise ValueError('API backend requires an explicit api_profile')
        from .api_model import ApiModelClient
        return ApiModelClient(root,api_profile)
    raise ValueError('answer_backend must be local, codex or api')
