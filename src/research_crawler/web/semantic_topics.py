# Derived from Crawl4AI model_loader.load_text_multilabel_classifier.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and semantic-source-manifest.json.
# Changes: explicit pinned local files, CPU-only bounded batches, scores and metadata.
from pathlib import Path
import json


class LocalTopicClassifier:
    def __init__(self, root, *, threshold=0.5, max_length=64):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(2)
        self.torch = torch
        folder = Path(root)/'models/semantic/tweet-topic-21-multi'
        self.metadata = json.loads((Path(root)/'runtime/semantic-topic-manifest.json').read_text(encoding='utf-8'))
        from .semantic_embeddings import verify_files
        verify_files(Path(root), self.metadata)
        self.tokenizer = AutoTokenizer.from_pretrained(folder,local_files_only=True)
        self.model = AutoModelForSequenceClassification.from_pretrained(folder,local_files_only=True,weights_only=True).to('cpu').eval()
        self.threshold = threshold
        self.max_length = max_length

    def __call__(self, texts):
        result = []
        for offset in range(0,len(texts),4):
            lengths = [len(ids) for ids in self.tokenizer(texts[offset:offset+4],truncation=False)['input_ids']]
            tokens = self.tokenizer(texts[offset:offset+4],return_tensors='pt',padding=True,
                                    truncation=True,max_length=self.max_length)
            with self.torch.inference_mode():
                scores = self.torch.sigmoid(self.model(**tokens).logits).cpu().tolist()
            for row, length in zip(scores,lengths,strict=True):
                named = {self.model.config.id2label[i]:value for i,value in enumerate(row)}
                result.append({'labels':[key for key,value in named.items() if value>=self.threshold],
                    'scores':named,'model':self.metadata['model'],'revision':self.metadata['revision'],
                    'threshold':self.threshold,'max_tokens':self.max_length,'language':'en',
                    'label_count':len(named),'truncated':length>self.max_length,
                    'model_claim':True,'semantic_accuracy_automatically_proven':False})
        return result
