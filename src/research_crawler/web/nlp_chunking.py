# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and semantic-source-manifest.json.
# Changes: explicit local-only data path, language parameter, fixed nl.toknize typo,
# consistent lowercase stopwords, and exact normalized-token source mappings.
from __future__ import annotations
from collections import Counter
from bisect import bisect_left
import re
import string
from pathlib import Path


def _nltk(data_path):
    import nltk
    nltk.data.path[:] = [str(Path(data_path).resolve())]
    return nltk


def source_ranges(text: str, chunks: list[str]) -> list[dict]:
    """Exact offsets even when upstream word chunkers normalize whitespace.

    Each item maps output tokens to the original text. The broad span is an
    original excerpt, and is never represented as byte-identical to a normalized chunk.
    """
    tokens = list(re.finditer(r'\S+', text))
    values = [token.group() for token in tokens]
    starts = [token.start() for token in tokens]
    positions = {}
    for index, value in enumerate(values):
        positions.setdefault(value, []).append(index)
    result = []
    previous_start = 0
    for chunk in chunks:
        words = chunk.split()
        matched = None
        if words:
            candidates = positions.get(words[0], [])
            first_token = bisect_left(starts, previous_start)
            candidate_offset = bisect_left(candidates, first_token)
            # Seek only occurrences of this chunk's first word after the cursor.
            # Stop at the first matching sequence, including overlapping windows.
            for offset in range(candidate_offset, len(candidates)):
                index = candidates[offset]
                if index + len(words) > len(tokens):
                    break
                if all(values[index+j] == word for j, word in enumerate(words)):
                    matched = index
                    break
        if matched is not None:
            selected = tokens[matched:matched+len(words)]
            start, end = selected[0].start(), selected[-1].end()
            # Preserve exact leading/trailing whitespace for identity and regex
            # chunks when it belongs to this same occurrence.
            # Search only up to this candidate, not the remaining document.
            # This also preserves an earlier regex substring inside a token.
            candidate_end = end + (len(chunk)-len(chunk.rstrip()))
            exact = text.find(chunk, previous_start, candidate_end)
            if exact >= 0:
                start, end = exact, exact+len(chunk)
                exact_match = True
                mapped = [{'start':start,'end':end}]
            else:
                mapped = [{'start':token.start(),'end':token.end()} for token in selected]
                exact_match = text[start:end] == chunk
        else:
            # Regex chunkers can split within a token, so a literal substring
            # fallback is necessary when no whole-token sequence can match.
            exact = text.find(chunk, previous_start) if chunk else -1
            if exact < 0:
                result.append({'mapped':False, 'reason':'chunk_not_found_in_source'})
                continue
            start, end = exact, exact+len(chunk)
            mapped = [{'start':start,'end':end}]
            exact_match = True
        result.append({'mapped':True,'start':start,'end':end,'unit':'unicode_code_points',
                       'end_exclusive':True,'exact_match':exact_match,'token_ranges':mapped})
        # Permit overlap, but disambiguate repeated chunks by advancing one code point.
        previous_start = start + 1
    return result


class NlpSentenceChunking:
    def __init__(self, *, data_path, language='english', **kwargs):
        self.nltk = _nltk(data_path)
        self.language = language
        self.nltk.data.find(f'tokenizers/punkt_tab/{language}/')

    def chunk(self, text: str) -> list[str]:
        return [sentence.strip() for sentence in self.nltk.tokenize.sent_tokenize(text, language=self.language)]


class TopicSegmentationChunking:
    def __init__(self, num_keywords=3, *, data_path, **kwargs):
        self.nltk = _nltk(data_path)
        self.stopwords = set(self.nltk.corpus.stopwords.words('english'))
        self.tokenizer = self.nltk.tokenize.TextTilingTokenizer(**{
            key:value for key,value in kwargs.items() if key in {'w','k','smoothing_width','smoothing_rounds','cutoff_policy'}})
        self.num_keywords = num_keywords

    def chunk(self, text: str) -> list[str]:
        return self.tokenizer.tokenize(text)

    def extract_keywords(self, text: str) -> list[str]:
        tokens = self.nltk.tokenize.word_tokenize(text)
        tokens = [token.lower() for token in tokens
                  if token.lower() not in self.stopwords and token not in string.punctuation]
        return [word for word,_ in Counter(tokens).most_common(self.num_keywords)]

    def chunk_with_topics(self, text: str) -> list:
        return [(segment, self.extract_keywords(segment)) for segment in self.chunk(text)]
