# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
from abc import ABC, abstractmethod
import re


class ChunkingStrategy(ABC):
    """
    Abstract base class for chunking strategies.
    """

    @abstractmethod
    def chunk(self, text: str) -> list:
        """
        Abstract method to chunk the given text.

        Args:
            text (str): The text to chunk.

        Returns:
            list: A list of chunks.
        """
        pass


class IdentityChunking(ChunkingStrategy):
    """
    Chunking strategy that returns the input text as a single chunk.
    """

    def chunk(self, text: str) -> list:
        return [text]


class RegexChunking(ChunkingStrategy):
    """
    Chunking strategy that splits text based on regular expression patterns.
    """

    def __init__(self, patterns=None, **kwargs):
        """
        Initialize the RegexChunking object.

        Args:
            patterns (list): A list of regular expression patterns to split text.
        """
        if patterns is None:
            patterns = [r"\n\n"]  # Default split pattern
        self.patterns = patterns

    def chunk(self, text: str) -> list:
        paragraphs = [text]
        for pattern in self.patterns:
            new_paragraphs = []
            for paragraph in paragraphs:
                new_paragraphs.extend(re.split(pattern, paragraph))
            paragraphs = new_paragraphs
        return paragraphs


class FixedLengthWordChunking(ChunkingStrategy):
    """
    Chunking strategy that splits text into fixed-length word chunks.

    How it works:
    1. Split the text into words
    2. Create chunks of fixed length
    3. Return the list of chunks
    """

    def __init__(self, chunk_size=100, **kwargs):
        """
        Initialize the fixed-length word chunking strategy with the given chunk size.

        Args:
            chunk_size (int): The size of each chunk in words.
        """
        self.chunk_size = chunk_size

    def chunk(self, text: str) -> list:
        words = text.split()
        return [
            " ".join(words[i : i + self.chunk_size])
            for i in range(0, len(words), self.chunk_size)
        ]


class SlidingWindowChunking(ChunkingStrategy):
    """
    Chunking strategy that splits text into overlapping word chunks.

    How it works:
    1. Split the text into words
    2. Create chunks of fixed length
    3. Return the list of chunks
    """

    def __init__(self, window_size=100, step=50, **kwargs):
        """
        Initialize the sliding window chunking strategy with the given window size and
        step size.

        Args:
            window_size (int): The size of the sliding window in words.
            step (int): The step size for sliding the window in words.
        """
        self.window_size = window_size
        self.step = step

    def chunk(self, text: str) -> list:
        words = text.split()
        chunks = []

        if len(words) <= self.window_size:
            return [text]

        for i in range(0, len(words) - self.window_size + 1, self.step):
            chunk = " ".join(words[i : i + self.window_size])
            chunks.append(chunk)

        # Handle the last chunk if it doesn't align perfectly
        if i + self.window_size < len(words):
            chunks.append(" ".join(words[-self.window_size :]))

        return chunks


class OverlappingWindowChunking(ChunkingStrategy):
    """
    Chunking strategy that splits text into overlapping word chunks.

    How it works:
    1. Split the text into words using whitespace
    2. Create chunks of fixed length equal to the window size
    3. Slide the window by the overlap size
    4. Return the list of chunks
    """

    def __init__(self, window_size=1000, overlap=100, **kwargs):
        """
        Initialize the overlapping window chunking strategy with the given window size and
        overlap size.

        Args:
            window_size (int): The size of the window in words.
            overlap (int): The size of the overlap between consecutive chunks in words.
        """
        self.window_size = window_size
        self.overlap = overlap

    def chunk(self, text: str) -> list:
        words = text.split()
        chunks = []

        if len(words) <= self.window_size:
            return [text]

        start = 0
        while start < len(words):
            end = start + self.window_size
            chunk = " ".join(words[start:end])
            chunks.append(chunk)

            if end >= len(words):
                break

            start = end - self.overlap

        return chunks
