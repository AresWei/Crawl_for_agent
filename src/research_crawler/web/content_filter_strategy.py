# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Tuple, Dict, Optional
from collections import deque
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
import re, math, warnings


class RelevantContentFilter(ABC):
    """Abstract base class for content filtering strategies"""

    def __init__(
        self,
        user_query: str = None,
        verbose: bool = False,
        logger: Optional[AsyncLogger] = None,
    ):
        """
        Initializes the RelevantContentFilter class with optional user query.

        Args:
            user_query (str): User query for filtering (optional).
            verbose (bool): Enable verbose logging (default: False).
        """
        self.user_query = user_query
        self.included_tags = {
            # Primary structure
            "article",
            "main",
            "section",
            "div",
            # List structures
            "ul",
            "ol",
            "li",
            "dl",
            "dt",
            "dd",
            # Text content
            "p",
            "span",
            "blockquote",
            "pre",
            "code",
            # Headers
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            # Tables
            "table",
            "thead",
            "tbody",
            "tr",
            "td",
            "th",
            # Other semantic elements
            "figure",
            "figcaption",
            "details",
            "summary",
            # Text formatting
            "em",
            "strong",
            "b",
            "i",
            "mark",
            "small",
            # Rich content
            "time",
            "address",
            "cite",
            "q",
        }
        self.excluded_tags = {
            "nav",
            "footer",
            "header",
            "aside",
            "script",
            "style",
            "form",
            "iframe",
            "noscript",
        }
        self.header_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
        self.negative_patterns = re.compile(
            r"nav|footer|header|sidebar|ads|comment|promo|advert|social|share", re.I
        )
        self.min_word_count = 2
        self.verbose = False
        self.logger = logger

    @abstractmethod
    def filter_content(self, html: str) -> List[str]:
        """Abstract method to be implemented by specific filtering strategies"""
        pass

    def extract_page_query(self, soup: BeautifulSoup, body: Tag) -> str:
        """Common method to extract page metadata with fallbacks"""
        if self.user_query:
            return self.user_query

        query_parts = []

        # Title
        try:
            title = soup.title.string
            if title:
                query_parts.append(title)
        except Exception:
            pass

        if soup.find("h1"):
            query_parts.append(soup.find("h1").get_text())

        # Meta tags
        temp = ""
        for meta_name in ["keywords", "description"]:
            meta = soup.find("meta", attrs={"name": meta_name})
            if meta and meta.get("content"):
                query_parts.append(meta["content"])
                temp += meta["content"]

        # If still empty, grab first significant paragraph
        if not temp:
            # Find the first tag P thatits text contains more than 50 characters
            for p in body.find_all("p"):
                if len(p.get_text()) > 150:
                    query_parts.append(p.get_text()[:150])
                    break

        return " ".join(filter(None, query_parts))

    def extract_text_chunks(
        self, body: Tag, min_word_threshold: int = None
    ) -> List[Tuple[str, str]]:
        """
        Extracts text chunks from a BeautifulSoup body element while preserving order.
        Returns list of tuples (text, tag_name) for classification.

        Args:
            body: BeautifulSoup Tag object representing the body element

        Returns:
            List of (text, tag_name) tuples
        """
        # Tags to ignore - inline elements that shouldn't break text flow
        INLINE_TAGS = {
            "a",
            "abbr",
            "acronym",
            "b",
            "bdo",
            "big",
            "br",
            "button",
            "cite",
            "code",
            "dfn",
            "em",
            "i",
            "img",
            "input",
            "kbd",
            "label",
            "map",
            "object",
            "q",
            "samp",
            "script",
            "select",
            "small",
            "span",
            "strong",
            "sub",
            "sup",
            "textarea",
            "time",
            "tt",
            "var",
        }

        # Tags that typically contain meaningful headers
        HEADER_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "header"}

        chunks = []
        current_text = []
        chunk_index = 0

        def should_break_chunk(tag: Tag) -> bool:
            """Determine if a tag should cause a break in the current text chunk"""
            return tag.name not in INLINE_TAGS and not (
                tag.name == "p" and len(current_text) == 0
            )

        # Use deque for efficient push/pop operations
        stack = deque([(body, False)])

        while stack:
            element, visited = stack.pop()

            if visited:
                # End of block element - flush accumulated text
                if current_text and should_break_chunk(element):
                    text = " ".join("".join(current_text).split())
                    if text:
                        tag_type = (
                            "header" if element.name in HEADER_TAGS else "content"
                        )
                        chunks.append((chunk_index, text, tag_type, element))
                        chunk_index += 1
                    current_text = []
                continue

            if isinstance(element, NavigableString):
                if str(element).strip():
                    current_text.append(str(element).strip())
                continue

            # Pre-allocate children to avoid multiple list operations
            children = list(element.children)
            if not children:
                continue

            # Mark block for revisit after processing children
            stack.append((element, True))

            # Add children in reverse order for correct processing
            for child in reversed(children):
                if isinstance(child, (Tag, NavigableString)):
                    stack.append((child, False))

        # Handle any remaining text
        if current_text:
            text = " ".join("".join(current_text).split())
            if text:
                chunks.append((chunk_index, text, "content", body))

        if min_word_threshold:
            chunks = [
                chunk for chunk in chunks if len(chunk[1].split()) >= min_word_threshold
            ]

        return chunks

    def _deprecated_extract_text_chunks(
        self, soup: BeautifulSoup
    ) -> List[Tuple[int, str, Tag]]:
        """Common method for extracting text chunks"""
        _text_cache = {}

        def fast_text(element: Tag) -> str:
            elem_id = id(element)
            if elem_id in _text_cache:
                return _text_cache[elem_id]
            texts = []
            for content in element.contents:
                if isinstance(content, str):
                    text = content.strip()
                    if text:
                        texts.append(text)
            result = " ".join(texts)
            _text_cache[elem_id] = result
            return result

        candidates = []
        index = 0

        def dfs(element):
            nonlocal index
            if isinstance(element, Tag):
                if element.name in self.included_tags:
                    if not self.is_excluded(element):
                        text = fast_text(element)
                        word_count = len(text.split())

                        # Headers pass through with adjusted minimum
                        if element.name in self.header_tags:
                            if word_count >= 3:  # Minimal sanity check for headers
                                candidates.append((index, text, element))
                                index += 1
                        # Regular content uses standard minimum
                        elif word_count >= self.min_word_count:
                            candidates.append((index, text, element))
                            index += 1

                for child in element.children:
                    dfs(child)

        dfs(soup.body if soup.body else soup)
        return candidates

    def is_excluded(self, tag: Tag) -> bool:
        """Common method for exclusion logic"""
        if tag.name in self.excluded_tags:
            return True
        class_id = " ".join(
            filter(None, [" ".join(tag.get("class", [])), tag.get("id", "")])
        )
        return bool(self.negative_patterns.search(class_id))

    def clean_element(self, tag: Tag) -> str:
        """Common method for cleaning HTML elements with minimal overhead"""
        if not tag or not isinstance(tag, Tag):
            return ""

        unwanted_tags = {"script", "style", "aside", "form", "iframe", "noscript"}
        unwanted_attrs = {
            "style",
            "onclick",
            "onmouseover",
            "align",
            "bgcolor",
            "class",
            "id",
        }

        # Use string builder pattern for better performance
        builder = []

        def render_tag(elem):
            if not isinstance(elem, Tag):
                if isinstance(elem, str):
                    builder.append(elem.strip())
                return

            if elem.name in unwanted_tags:
                return

            # Start tag
            builder.append(f"<{elem.name}")

            # Add cleaned attributes
            attrs = {k: v for k, v in elem.attrs.items() if k not in unwanted_attrs}
            for key, value in attrs.items():
                builder.append(f' {key}="{value}"')

            builder.append(">")

            # Process children
            for child in elem.children:
                render_tag(child)

            # Close tag
            builder.append(f"</{elem.name}>")

        try:
            render_tag(tag)
            return "".join(builder)
        except Exception:
            return str(tag)  # Fallback to original if anything fails


class PruningContentFilter(RelevantContentFilter):
    """
    Content filtering using pruning algorithm with dynamic threshold.

    How it works:
    1. Extracts page metadata with fallbacks.
    2. Extracts text chunks from the body element.
    3. Applies pruning algorithm to calculate scores for each chunk.
    4. Filters out chunks below the threshold.
    5. Sorts chunks by score in descending order.
    6. Returns the top N chunks.

    Attributes:
        user_query (str): User query for filtering (optional), if not provided, falls back to page metadata.
        min_word_threshold (int): Minimum word threshold for filtering (optional).
        threshold_type (str): Threshold type for dynamic threshold (default: 'fixed').
        threshold (float): Fixed threshold value (default: 0.48).

        Methods:
            filter_content(self, html: str, min_word_threshold: int = None):
    """

    def __init__(
        self,
        user_query: str = None,
        min_word_threshold: int = None,
        threshold_type: str = "fixed",
        threshold: float = 0.48,
        preserve_classes: list = None,
        preserve_tags: list = None,
    ):
        """
        Initializes the PruningContentFilter class, if not provided, falls back to page metadata.

        Note:
        If no query is given and no page metadata is available, then it tries to pick up the first significant paragraph.

        Args:
            user_query (str): User query for filtering (optional).
            min_word_threshold (int): Minimum word threshold for filtering (optional).
            threshold_type (str): Threshold type for dynamic threshold (default: 'fixed').
            threshold (float): Fixed threshold value (default: 0.48).
            preserve_classes (list): CSS class names to always keep regardless of score (optional).
            preserve_tags (list): HTML tag names to always keep regardless of score (optional).
        """
        super().__init__(None)
        # Deprecation: the BeautifulSoup engine is superseded by the lxml one.
        # Only warn for direct use of this class, not when PruningContentFilterLXML
        # (a subclass) calls super().__init__.
        if type(self) is PruningContentFilter:
            warnings.warn(
                "PruningContentFilter (BeautifulSoup-based) is deprecated in favor of "
                "PruningContentFilterLXML, an lxml reimplementation that is ~10x faster "
                "with identical output. Switch to PruningContentFilterLXML now. In an "
                "upcoming release PruningContentFilter will become an alias for the lxml "
                "implementation, and PruningContentFilterLXML will be kept as a legacy alias.",
                DeprecationWarning,
                stacklevel=2,
            )
        self.min_word_threshold = min_word_threshold
        self.threshold_type = threshold_type
        self.threshold = threshold
        self.preserve_classes = set(preserve_classes) if preserve_classes else set()
        self.preserve_tags = set(preserve_tags) if preserve_tags else set()

        # Add tag importance for dynamic threshold
        self.tag_importance = {
            "article": 1.5,
            "main": 1.4,
            "section": 1.3,
            "p": 1.2,
            "h1": 1.4,
            "h2": 1.3,
            "h3": 1.2,
            "div": 0.7,
            "span": 0.6,
        }

        # Metric configuration
        self.metric_config = {
            "text_density": True,
            "link_density": True,
            "tag_weight": True,
            "class_id_weight": True,
            "text_length": True,
        }

        self.metric_weights = {
            "text_density": 0.4,
            "link_density": 0.2,
            "tag_weight": 0.2,
            "class_id_weight": 0.1,
            "text_length": 0.1,
        }

        self.tag_weights = {
            "div": 0.5,
            "p": 1.0,
            "article": 1.5,
            "section": 1.0,
            "span": 0.3,
            "li": 0.5,
            "ul": 0.5,
            "ol": 0.5,
            "h1": 1.2,
            "h2": 1.1,
            "h3": 1.0,
            "h4": 0.9,
            "h5": 0.8,
            "h6": 0.7,
        }

    def filter_content(self, html: str, min_word_threshold: int = None) -> List[str]:
        """
        Implements content filtering using pruning algorithm with dynamic threshold.

        Note:
        This method implements the filtering logic for the PruningContentFilter class.
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
        if not soup.body:
            soup = BeautifulSoup(f"<body>{html}</body>", "lxml")

        # Remove comments and unwanted tags
        self._remove_comments(soup)
        self._remove_unwanted_tags(soup)

        # Prune tree starting from body
        body = soup.find("body")
        self._prune_tree(body)

        # Extract remaining content as list of HTML strings
        content_blocks = []
        for element in body.children:
            if isinstance(element, str) or not hasattr(element, "name"):
                continue
            if len(element.get_text(strip=True)) > 0:
                content_blocks.append(str(element))

        return content_blocks

    def _remove_comments(self, soup):
        """Removes HTML comments"""
        for element in soup(string=lambda string: isinstance(string, Comment)):
            element.extract()

    def _remove_unwanted_tags(self, soup):
        """Removes unwanted tags"""
        for tag in self.excluded_tags:
            for element in soup.find_all(tag):
                element.decompose()

    def _is_preserved(self, node):
        """Check if a node matches the preserve whitelist."""
        if self.preserve_tags and node.name in self.preserve_tags:
            return True
        if self.preserve_classes and "class" in getattr(node, "attrs", {}):
            node_classes = set(node["class"]) if isinstance(node["class"], list) else {node["class"]}
            if node_classes & self.preserve_classes:
                return True
        return False

    def _prune_tree(self, node):
        """
        Prunes the tree starting from the given node.

        Args:
            node (Tag): The node from which the pruning starts.
        """
        if not node or not hasattr(node, "name") or node.name is None:
            return

        # Skip pruning for preserved nodes — always keep them
        if self._is_preserved(node):
            return

        text_len = len(node.get_text(strip=True))
        tag_len = len(node.encode_contents().decode("utf-8"))
        link_text_len = sum(
            len(s.strip())
            for s in (a.string for a in node.find_all("a", recursive=False))
            if s
        )

        metrics = {
            "node": node,
            "tag_name": node.name,
            "text_len": text_len,
            "tag_len": tag_len,
            "link_text_len": link_text_len,
        }

        score = self._compute_composite_score(metrics, text_len, tag_len, link_text_len)

        if self.threshold_type == "fixed":
            should_remove = score < self.threshold
        else:  # dynamic
            tag_importance = self.tag_importance.get(node.name, 0.7)
            text_ratio = text_len / tag_len if tag_len > 0 else 0
            link_ratio = link_text_len / text_len if text_len > 0 else 1

            threshold = self.threshold  # base threshold
            if tag_importance > 1:
                threshold *= 0.8
            if text_ratio > 0.4:
                threshold *= 0.9
            if link_ratio > 0.6:
                threshold *= 1.2

            should_remove = score < threshold

        if should_remove:
            node.decompose()
        else:
            children = [child for child in node.children if hasattr(child, "name")]
            for child in children:
                self._prune_tree(child)

    def _compute_composite_score(self, metrics, text_len, tag_len, link_text_len):
        """Computes the composite score"""
        if self.min_word_threshold:
            # Get raw text from metrics node - avoid extra processing
            text = metrics["node"].get_text(strip=True)
            word_count = text.count(" ") + 1
            if word_count < self.min_word_threshold:
                return -1.0  # Guaranteed removal
        score = 0.0
        total_weight = 0.0

        if self.metric_config["text_density"]:
            density = text_len / tag_len if tag_len > 0 else 0
            score += self.metric_weights["text_density"] * density
            total_weight += self.metric_weights["text_density"]

        if self.metric_config["link_density"]:
            density = 1 - (link_text_len / text_len if text_len > 0 else 0)
            score += self.metric_weights["link_density"] * density
            total_weight += self.metric_weights["link_density"]

        if self.metric_config["tag_weight"]:
            tag_score = self.tag_weights.get(metrics["tag_name"], 0.5)
            score += self.metric_weights["tag_weight"] * tag_score
            total_weight += self.metric_weights["tag_weight"]

        if self.metric_config["class_id_weight"]:
            class_score = self._compute_class_id_weight(metrics["node"])
            score += self.metric_weights["class_id_weight"] * max(0, class_score)
            total_weight += self.metric_weights["class_id_weight"]

        if self.metric_config["text_length"]:
            score += self.metric_weights["text_length"] * math.log(text_len + 1)
            total_weight += self.metric_weights["text_length"]

        return score / total_weight if total_weight > 0 else 0

    def _compute_class_id_weight(self, node):
        """Computes the class ID weight"""
        class_id_score = 0
        if "class" in node.attrs:
            classes = " ".join(node["class"])
            if self.negative_patterns.match(classes):
                class_id_score -= 0.5
        if "id" in node.attrs:
            element_id = node["id"]
            if self.negative_patterns.match(element_id):
                class_id_score -= 0.5
        return class_id_score
