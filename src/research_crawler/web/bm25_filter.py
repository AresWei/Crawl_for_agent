# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
from typing import List
from bs4 import BeautifulSoup
from rank_bm25 import BM25Okapi
from snowballstemmer import stemmer
from .content_filter_strategy import RelevantContentFilter
from .filter_utils import clean_tokens


class BM25ContentFilter(RelevantContentFilter):
    """
    Content filtering using BM25 algorithm with priority tag handling.

    How it works:
    1. Extracts page metadata with fallbacks.
    2. Extracts text chunks from the body element.
    3. Tokenizes the corpus and query.
    4. Applies BM25 algorithm to calculate scores for each chunk.
    5. Filters out chunks below the threshold.
    6. Sorts chunks by score in descending order.
    7. Returns the top N chunks.

    Attributes:
        user_query (str): User query for filtering (optional).
        bm25_threshold (float): BM25 threshold for filtering (default: 1.0).
        language (str): Language for stemming (default: 'english').

        Methods:
            filter_content(self, html: str, min_word_threshold: int = None)
    """

    def __init__(
        self,
        user_query: str = None,
        bm25_threshold: float = 1.0,
        language: str = "english",
        use_stemming: bool = True,
    ):
        """
        Initializes the BM25ContentFilter class, if not provided, falls back to page metadata.

        Note:
        If no query is given and no page metadata is available, then it tries to pick up the first significant paragraph.

        Args:
            user_query (str): User query for filtering (optional).
            bm25_threshold (float): BM25 threshold for filtering (default: 1.0).
            language (str): Language for stemming (default: 'english').
            use_stemming (bool): Whether to apply stemming (default: True).
        """
        super().__init__(user_query=user_query)
        self.bm25_threshold = bm25_threshold
        self.use_stemming = use_stemming
        self.priority_tags = {
            "h1": 5.0,
            "h2": 4.0,
            "h3": 3.0,
            "title": 4.0,
            "strong": 2.0,
            "b": 1.5,
            "em": 1.5,
            "blockquote": 2.0,
            "code": 2.0,
            "pre": 1.5,
            "th": 1.5,  # Table headers
        }
        self.stemmer = stemmer(language) if use_stemming else None

    def filter_content(self, html: str, min_word_threshold: int = None) -> List[str]:
        """
        Implements content filtering using BM25 algorithm with priority tag handling.

            Note:
        This method implements the filtering logic for the BM25ContentFilter class.
        It takes HTML content as input and returns a list of filtered text chunks.

        Args:
            html (str): HTML content to be filtered.
            min_word_threshold (int): Minimum word threshold for filtering (optional).

        Returns:
            List[str]: List of filtered text chunks.
        """
        if not html or not isinstance(html, str):
            return []

        soup = BeautifulSoup(html, "lxml")

        # Check if body is present
        if not soup.body:
            # Wrap in body tag if missing
            soup = BeautifulSoup(f"<body>{html}</body>", "lxml")
        body = soup.find("body")

        query = self.extract_page_query(soup, body)

        if not query:
            return []
            # return [self.clean_element(soup)]

        candidates = self.extract_text_chunks(body, min_word_threshold)

        if not candidates:
            return []

        # Tokenize corpus
        # tokenized_corpus = [chunk.lower().split() for _, chunk, _, _ in candidates]
        # tokenized_query = query.lower().split()

        # tokenized_corpus = [[ps.stem(word) for word in chunk.lower().split()]
        #                 for _, chunk, _, _ in candidates]
        # tokenized_query = [ps.stem(word) for word in query.lower().split()]

        if self.use_stemming:
            tokenized_corpus = [
                [self.stemmer.stemWord(word) for word in chunk.lower().split()]
                for _, chunk, _, _ in candidates
            ]
            tokenized_query = [
                self.stemmer.stemWord(word) for word in query.lower().split()
            ]
        else:
            tokenized_corpus = [
                chunk.lower().split() for _, chunk, _, _ in candidates
            ]
            tokenized_query = query.lower().split()

        # tokenized_corpus = [[self.stemmer.stemWord(word) for word in tokenize_text(chunk.lower())]
        #            for _, chunk, _, _ in candidates]
        # tokenized_query = [self.stemmer.stemWord(word) for word in tokenize_text(query.lower())]

        # Clean from stop words and noise
        tokenized_corpus = [clean_tokens(tokens) for tokens in tokenized_corpus]
        tokenized_query = clean_tokens(tokenized_query)

        bm25 = BM25Okapi(tokenized_corpus)
        scores = bm25.get_scores(tokenized_query)

        # Adjust scores with tag weights
        adjusted_candidates = []
        for score, (index, chunk, tag_type, tag) in zip(scores, candidates):
            tag_weight = self.priority_tags.get(tag.name, 1.0)
            adjusted_score = score * tag_weight
            adjusted_candidates.append((adjusted_score, index, chunk, tag))

        # Filter candidates by threshold
        selected_candidates = [
            (index, chunk, tag)
            for adjusted_score, index, chunk, tag in adjusted_candidates
            if adjusted_score >= self.bm25_threshold
        ]

        if not selected_candidates:
            return []

        # Sort selected candidates by original document order
        selected_candidates.sort(key=lambda x: x[0])

        # Deduplicate by chunk text, keeping first occurrence (lowest index)
        seen_texts = set()
        unique_candidates = []
        for index, chunk, tag in selected_candidates:
            if chunk not in seen_texts:
                seen_texts.add(chunk)
                unique_candidates.append((index, chunk, tag))

        return [self.clean_element(tag) for _, _, tag in unique_candidates]
