"""Explicit account-backed or fully local text execution; no paid API fallback."""
from .local_model import LocalModelClient


def text_client(root, backend='local'):
    if backend == 'local':return LocalModelClient(root)
    if backend == 'codex':
        from .codex_model import CodexModelClient
        return CodexModelClient(root)
    raise ValueError('answer_backend must be local or codex')
