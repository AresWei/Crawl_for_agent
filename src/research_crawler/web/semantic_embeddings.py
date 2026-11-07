"""Local CPU sentence embeddings. No network or model downloads at inference."""
from __future__ import annotations
import json
import hashlib
from pathlib import Path


def verify_files(root: Path, manifest: dict):
    for entry in manifest['files']:
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('Model asset must stay inside project root')
        with path.open('rb') as source:
            actual = hashlib.file_digest(source,'sha256').hexdigest()
        if path.stat().st_size != entry['bytes'] or actual != entry['sha256']:
            raise ValueError('Model asset identity mismatch')


class OnnxEmbeddings:
    def __init__(self, root: str | Path, *, threads: int = 2, max_tokens: int = 128):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.root = Path(root)
        self.manifest = json.loads((self.root / 'runtime/semantic-model-manifest.json').read_text(encoding='utf-8'))
        verify_files(self.root, self.manifest)
        folder = self.root / 'models/semantic/multilingual-minilm'
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, min(threads, 4))
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(folder / 'onnx/model_quint8_avx2.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])
        self.tokenizer = Tokenizer.from_file(str(folder / 'tokenizer.json'))
        self.max_tokens = min(max_tokens, 128)
        self.tokenizer.enable_truncation(max_length=self.max_tokens)
        self.tokenizer.enable_padding(pad_id=0, pad_token='<pad>')
        self.truncated_inputs = 0
        self.input_coverage = []
        self.last_input_coverage = []

    def encode(self, texts: list[str], batch_size: int = 8):
        import numpy as np
        if not texts:
            self.last_input_coverage = []
            return np.empty((0, 384), dtype=np.float32)
        results = []
        coverage = []
        for offset in range(0, len(texts), batch_size):
            encoded = self.tokenizer.encode_batch(texts[offset:offset + batch_size])
            self.truncated_inputs += sum(bool(item.overflowing) for item in encoded)
            for text, item in zip(texts[offset:offset + batch_size],encoded,strict=True):
                source_offsets = [span for span,special,attention in zip(item.offsets,item.special_tokens_mask,item.attention_mask)
                                  if not special and attention]
                input_tokens = sum(item.attention_mask) + sum(
                    sum(attention and not special for attention,special in zip(extra.attention_mask,extra.special_tokens_mask))
                    for extra in item.overflowing)
                coverage.append({'text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    'input_characters':len(text),'input_tokens':input_tokens,'encoded_tokens':sum(item.attention_mask),
                    'truncated':bool(item.overflowing),'encoded_character_range':{
                        'start':min((span[0] for span in source_offsets),default=0),
                        'end':max((span[1] for span in source_offsets),default=0),
                        'unit':'unicode_code_points','end_exclusive':True}})
            inputs = {'input_ids':np.asarray([x.ids for x in encoded], dtype=np.int64),
                'attention_mask':np.asarray([x.attention_mask for x in encoded], dtype=np.int64),
                'token_type_ids':np.asarray([x.type_ids for x in encoded], dtype=np.int64)}
            feed = {x.name:inputs[x.name] for x in self.session.get_inputs()}
            output = self.session.run(None, feed)[0]
            mask = inputs['attention_mask'][..., None]
            pooled = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
            results.append(pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12))
        self.last_input_coverage = coverage
        self.input_coverage.extend(coverage)
        return np.vstack(results).astype(np.float32)

    def metadata(self):
        return {key:self.manifest[key] for key in ('model','revision','license','backend','pooling','max_tokens','dimensions')} | {
            'truncated_inputs': self.truncated_inputs, 'network_used':False, 'gpu_used':False,
            'weights_sha256':next(item['sha256'] for item in self.manifest['files'] if item['path'].endswith('.onnx')),
            'tokenizer_sha256':next(item['sha256'] for item in self.manifest['files'] if item['path'].endswith('/tokenizer.json')),
            'implementation_version':'onnx-masked-mean-l2-v1',
            'manifest_path':'runtime/semantic-model-manifest.json'}
