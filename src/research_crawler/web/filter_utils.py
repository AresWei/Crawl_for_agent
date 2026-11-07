# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
import re


def clean_tokens(tokens: list[str]) -> list[str]:
    """
    Clean a list of tokens by removing noise, stop words, and short tokens.

    How it works:
    1. Defines a set of noise words and stop words.
    2. Filters tokens based on length and exclusion criteria.
    3. Excludes tokens starting with certain symbols (e.g., "↑", "▲").

    Args:
        tokens (list[str]): The list of tokens to clean.

    Returns:
        list[str]: The cleaned list of tokens.
    """

    # Set of tokens to remove
    noise = {
        "ccp",
        "up",
        "↑",
        "▲",
        "⬆️",
        "a",
        "an",
        "at",
        "by",
        "in",
        "of",
        "on",
        "to",
        "the",
    }

    STOP_WORDS = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "he",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        # Pronouns
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "her",
        "its",
        "our",
        "their",
        "mine",
        "yours",
        "hers",
        "ours",
        "theirs",
        "myself",
        "yourself",
        "himself",
        "herself",
        "itself",
        "ourselves",
        "themselves",
        # Common verbs
        "am",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "having",
        "do",
        "does",
        "did",
        "doing",
        # Prepositions
        "about",
        "above",
        "across",
        "after",
        "against",
        "along",
        "among",
        "around",
        "at",
        "before",
        "behind",
        "below",
        "beneath",
        "beside",
        "between",
        "beyond",
        "by",
        "down",
        "during",
        "except",
        "for",
        "from",
        "in",
        "inside",
        "into",
        "near",
        "of",
        "off",
        "on",
        "out",
        "outside",
        "over",
        "past",
        "through",
        "to",
        "toward",
        "under",
        "underneath",
        "until",
        "up",
        "upon",
        "with",
        "within",
        # Conjunctions
        "and",
        "but",
        "or",
        "nor",
        "for",
        "yet",
        "so",
        "although",
        "because",
        "since",
        "unless",
        # Articles
        "a",
        "an",
        "the",
        # Other common words
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "can",
        "cannot",
        "can't",
        "could",
        "couldn't",
        "may",
        "might",
        "must",
        "mustn't",
        "shall",
        "should",
        "shouldn't",
        "will",
        "won't",
        "would",
        "wouldn't",
        "not",
        "n't",
        "no",
        "nor",
        "none",
    }

    # Single comprehension, more efficient than multiple passes
    return [
        token
        for token in tokens
        if len(token) > 2
        and token not in noise
        and token not in STOP_WORDS
        and not token.startswith("↑")
        and not token.startswith("▲")
        and not token.startswith("⬆")
    ]
