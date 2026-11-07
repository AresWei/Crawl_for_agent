# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
from typing import Optional, Annotated, Dict, Any, List
from pydantic import BaseModel, BeforeValidator


def _coerce_int(v):
    """Coerce to int or return None for non-numeric values like '100%' or 'auto'."""
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


class MediaItem(BaseModel):
    src: Optional[str] = ""
    data: Optional[str] = ""
    alt: Optional[str] = ""
    desc: Optional[str] = ""
    score: Optional[int] = 0
    type: str = "image"
    group_id: Optional[int] = 0
    format: Optional[str] = None
    width: Annotated[Optional[int], BeforeValidator(_coerce_int)] = None


class Link(BaseModel):
    href: Optional[str] = ""
    text: Optional[str] = ""
    title: Optional[str] = ""
    base_domain: Optional[str] = ""
    head_data: Optional[Dict[str, Any]] = None  # Head metadata extracted from link target
    head_extraction_status: Optional[str] = None  # "success", "failed", "skipped"
    head_extraction_error: Optional[str] = None  # Error message if extraction failed
    intrinsic_score: Optional[float] = None  # Quality score based on URL structure, text, and context
    contextual_score: Optional[float] = None  # BM25 relevance score based on query and head content
    total_score: Optional[float] = None  # Combined score from intrinsic and contextual scores


class Media(BaseModel):
    images: List[MediaItem] = []
    videos: List[
        MediaItem
    ] = []  # Using MediaItem model for now, can be extended with Video model if needed
    audios: List[
        MediaItem
    ] = []  # Using MediaItem model for now, can be extended with Audio model if needed
    tables: List[Dict] = []  # Table data extracted from HTML tables


class Links(BaseModel):
    internal: List[Link] = []
    external: List[Link] = []


class ScrapingResult(BaseModel):
    cleaned_html: str
    success: bool
    media: Media = Media()
    links: Links = Links()
    metadata: Dict[str, Any] = {}
