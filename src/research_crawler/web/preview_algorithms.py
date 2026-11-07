# Source-selected Crawl4AI v0.9.5 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI authors; Apache-2.0 and additional attribution retained.
# See LICENSE.crawl4ai, NOTICE and preview-source-manifest.json.
from __future__ import annotations
import fnmatch
from typing import Any, Dict, List, Optional
from .scraping_models import Link, Links

def calculate_total_score(
    intrinsic_score: Optional[float] = None,
    contextual_score: Optional[float] = None,
    score_links_enabled: bool = False,
    query_provided: bool = False
) -> float:
    """
    Calculate combined total score from intrinsic and contextual scores with smart fallbacks.
    
    Args:
        intrinsic_score: Quality score based on URL structure, text, and context (0-10)
        contextual_score: BM25 relevance score based on query and head content (0-1 typically)
        score_links_enabled: Whether link scoring is enabled
        query_provided: Whether a query was provided for contextual scoring
        
    Returns:
        Combined total score (0-10 scale)
        
    Scoring Logic:
        - No scoring: return 5.0 (neutral score)
        - Only intrinsic: return normalized intrinsic score
        - Only contextual: return contextual score scaled to 10
        - Both: weighted combination (70% intrinsic, 30% contextual scaled)
    """
    # Case 1: No scoring enabled at all
    if not score_links_enabled:
        return 5.0  # Neutral score - all links treated equally
    
    # Normalize scores to handle None values
    intrinsic = intrinsic_score if intrinsic_score is not None else 0.0
    contextual = contextual_score if contextual_score is not None else 0.0
    
    # Case 2: Only intrinsic scoring (no query provided or no head extraction)
    if not query_provided or contextual_score is None:
        # Use intrinsic score directly (already 0-10 scale)
        return max(0.0, min(intrinsic, 10.0))
    
    # Case 3: Both intrinsic and contextual scores available
    # Scale contextual score (typically 0-1) to 0-10 range
    contextual_scaled = min(contextual * 10.0, 10.0)
    
    # Weighted combination: 70% intrinsic (structure/content quality) + 30% contextual (query relevance)
    # This gives more weight to link quality while still considering relevance
    total = (intrinsic * 0.7) + (contextual_scaled * 0.3)
    
    return max(0.0, min(total, 10.0))

class PreviewAlgorithms:
    logger = None

    def _log(self, level: str, message: str, tag: str = "LINK_EXTRACT", **kwargs):
            """Helper method to safely log messages."""
            if self.logger:
                log_method = getattr(self.logger, level, None)
                if log_method:
                    log_method(message=message, tag=tag, params=kwargs.get('params', {}))

    def _filter_links(self, links: Links, link_config: Dict[str, Any]) -> List[str]:
            """
            Filter links based on configuration parameters.
            
            Args:
                links: Links object containing internal and external links
                link_config: Configuration dictionary for link extraction
                
            Returns:
                List of filtered URL strings
            """
            filtered_urls = []
            
            # Include internal links if configured
            if link_config.include_internal:
                filtered_urls.extend([link.href for link in links.internal if link.href])
                self._log("debug", "Added {count} internal links",
                          params={"count": len(links.internal)})
            
            # Include external links if configured
            if link_config.include_external:
                filtered_urls.extend([link.href for link in links.external if link.href])
                self._log("debug", "Added {count} external links",
                          params={"count": len(links.external)})
            
            # Apply include patterns
            include_patterns = link_config.include_patterns
            if include_patterns:
                filtered_urls = [
                    url for url in filtered_urls
                    if any(fnmatch.fnmatch(url, pattern) for pattern in include_patterns)
                ]
                self._log("debug", "After include patterns: {count} links remain",
                          params={"count": len(filtered_urls)})
            
            # Apply exclude patterns
            exclude_patterns = link_config.exclude_patterns
            if exclude_patterns:
                filtered_urls = [
                    url for url in filtered_urls
                    if not any(fnmatch.fnmatch(url, pattern) for pattern in exclude_patterns)
                ]
                self._log("debug", "After exclude patterns: {count} links remain",
                          params={"count": len(filtered_urls)})
            
            # Limit number of links
            max_links = link_config.max_links
            if max_links > 0 and len(filtered_urls) > max_links:
                filtered_urls = filtered_urls[:max_links]
                self._log("debug", "Limited to {max_links} links",
                          params={"max_links": max_links})
            
            # Remove duplicates while preserving order
            seen = set()
            unique_urls = []
            for url in filtered_urls:
                if url not in seen:
                    seen.add(url)
                    unique_urls.append(url)
            
            self._log("debug", "Final filtered URLs: {count} unique links",
                      params={"count": len(unique_urls)})
            
            return unique_urls

    def _merge_head_data(
            self, 
            original_links: Links, 
            head_results: List[Dict[str, Any]],
            config: CrawlerRunConfig
        ) -> Links:
            """
            Merge head extraction results back into Link objects.
            
            Args:
                original_links: Original Links object
                head_results: Results from head extraction
                
            Returns:
                Links object with head_data attached to matching links
            """
            # Create URL to head_data mapping (key by both final and original URL for redirects)
            url_to_head_data = {}
            for result in head_results:
                url = result.get("url")
                head_info = {
                    "head_data": result.get("head_data", {}),
                    "status": result.get("status", "unknown"),
                    "error": result.get("error"),
                    "relevance_score": result.get("relevance_score")
                }
                if url:
                    url_to_head_data[url] = head_info
                original_url = result.get("original_url")
                if original_url and original_url != url:
                    url_to_head_data[original_url] = head_info
            
            # Update internal links
            updated_internal = []
            for link in original_links.internal:
                if link.href in url_to_head_data:
                    head_info = url_to_head_data[link.href]
                    # Create new Link object with head data and scoring
                    contextual_score = head_info.get("relevance_score")
                    
                    updated_link = Link(
                        href=link.href,
                        text=link.text,
                        title=link.title,
                        base_domain=link.base_domain,
                        head_data=head_info["head_data"],
                        head_extraction_status=head_info["status"],
                        head_extraction_error=head_info.get("error"),
                        intrinsic_score=getattr(link, 'intrinsic_score', None),
                        contextual_score=contextual_score
                    )
                    
                    # Add relevance score to head_data for backward compatibility
                    if contextual_score is not None:
                        updated_link.head_data = updated_link.head_data or {}
                        updated_link.head_data["relevance_score"] = contextual_score
                    
                    # Calculate total score combining intrinsic and contextual scores
                    updated_link.total_score = calculate_total_score(
                        intrinsic_score=updated_link.intrinsic_score,
                        contextual_score=updated_link.contextual_score,
                        score_links_enabled=getattr(config, 'score_links', False),
                        query_provided=bool(config.link_preview_config.query)
                    )
                    
                    updated_internal.append(updated_link)
                else:
                    # Calculate total_score even without head data, using intrinsic_score
                    link.total_score = calculate_total_score(
                        intrinsic_score=getattr(link, 'intrinsic_score', None),
                        contextual_score=None,
                        score_links_enabled=getattr(config, 'score_links', False),
                        query_provided=bool(config.link_preview_config.query)
                    )
                    updated_internal.append(link)

            # Update external links
            updated_external = []
            for link in original_links.external:
                if link.href in url_to_head_data:
                    head_info = url_to_head_data[link.href]
                    # Create new Link object with head data and scoring
                    contextual_score = head_info.get("relevance_score")
                    
                    updated_link = Link(
                        href=link.href,
                        text=link.text,
                        title=link.title,
                        base_domain=link.base_domain,
                        head_data=head_info["head_data"],
                        head_extraction_status=head_info["status"],
                        head_extraction_error=head_info.get("error"),
                        intrinsic_score=getattr(link, 'intrinsic_score', None),
                        contextual_score=contextual_score
                    )
                    
                    # Add relevance score to head_data for backward compatibility
                    if contextual_score is not None:
                        updated_link.head_data = updated_link.head_data or {}
                        updated_link.head_data["relevance_score"] = contextual_score
                    
                    # Calculate total score combining intrinsic and contextual scores
                    updated_link.total_score = calculate_total_score(
                        intrinsic_score=updated_link.intrinsic_score,
                        contextual_score=updated_link.contextual_score,
                        score_links_enabled=getattr(config, 'score_links', False),
                        query_provided=bool(config.link_preview_config.query)
                    )
                    
                    updated_external.append(updated_link)
                else:
                    # Calculate total_score even without head data, using intrinsic_score
                    link.total_score = calculate_total_score(
                        intrinsic_score=getattr(link, 'intrinsic_score', None),
                        contextual_score=None,
                        score_links_enabled=getattr(config, 'score_links', False),
                        query_provided=bool(config.link_preview_config.query)
                    )
                    updated_external.append(link)

            # Sort links by relevance score if available
            if any(hasattr(link, 'head_data') and link.head_data and 'relevance_score' in link.head_data 
                   for link in updated_internal + updated_external):
                
                def get_relevance_score(link):
                    if hasattr(link, 'head_data') and link.head_data and 'relevance_score' in link.head_data:
                        return link.head_data['relevance_score']
                    return 0.0
                
                updated_internal.sort(key=get_relevance_score, reverse=True)
                updated_external.sort(key=get_relevance_score, reverse=True)
            
            return Links(
                internal=updated_internal,
                external=updated_external
            )
