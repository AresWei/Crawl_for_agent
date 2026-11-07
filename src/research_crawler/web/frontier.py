"""Durable core frontier using the preserved upstream discovery/filter/scorer algorithms.

The core commits the entire pending frontier with each result. In-flight URLs remain
pending until their commit, unlike the upstream separate state-change callback.
"""
from types import SimpleNamespace
from urllib.parse import urlsplit

from .deep_crawling.bfs_strategy import BFSDeepCrawlStrategy
from .deep_crawling.dfs_strategy import DFSDeepCrawlStrategy
from .deep_crawling.bff_strategy import BestFirstCrawlingStrategy
from .deep_crawling import filters, scorers


def make_scorer(spec):
    if not spec:
        return None
    if isinstance(spec, list):
        return scorers.CompositeScorer([make_scorer(item) for item in spec])
    choices = {"keyword": scorers.KeywordRelevanceScorer, "path_depth": scorers.PathDepthScorer,
               "content_type": scorers.ContentTypeScorer, "freshness": scorers.FreshnessScorer,
               "domain_authority": scorers.DomainAuthorityScorer}
    kind = spec.get("type", "keyword")
    if kind not in choices:
        raise ValueError("Unknown URL scorer")
    return choices[kind](**{k: v for k, v in spec.items() if k != "type"})


class Frontier:
    def __init__(self, params, urls):
        self.kind = params.get("strategy", params.get("deep_strategy", "bfs")).lower().replace("-", "_")
        choices = {"bfs": BFSDeepCrawlStrategy, "dfs": DFSDeepCrawlStrategy, "best_first": BestFirstCrawlingStrategy}
        if self.kind not in choices:
            raise ValueError("strategy must be bfs, dfs or best_first")
        filter_types = {"url_pattern": filters.URLPatternFilter, "domain": filters.DomainFilter,
                        "content_type": filters.ContentTypeFilter, "relevance": filters.ContentRelevanceFilter,
                        "seo": filters.SEOFilter}
        chain = []
        for spec in params.get("url_filters", []):
            if spec.get("type") not in filter_types:
                raise ValueError("Unknown URL filter")
            chain.append(filter_types[spec["type"]](**{k: v for k, v in spec.items() if k != "type"}))
        self.scorer = make_scorer(params.get("url_scorer"))
        self.threshold = float(params.get("score_threshold", "-inf"))
        self.limit = max(1, min(10000, int(params.get("max_discovered_urls", 1000))))
        self.same_site = params.get("same_site", True)
        self.domains = {urlsplit(url).netloc for url in urls}
        self.policy = choices[self.kind](max_depth=max(0, min(50, int(params.get("max_depth", 0)))),
            filter_chain=filters.FilterChain(chain), url_scorer=self.scorer, include_external=not self.same_site,
            score_threshold=self.threshold, max_pages=float("inf"))

    def batch(self, state, capacity):
        # DFS must discover each child's branch before admitting its sibling.
        capacity = 1 if self.kind == "dfs" else capacity
        chosen, domains = [], set()
        for item in state["pending"]:
            domain = urlsplit(item["url"]).hostname
            if domain in domains:
                continue
            if self.kind == "bfs" and chosen and item["depth"] != chosen[0]["depth"]:
                break
            chosen.append(item)
            domains.add(domain)
            if len(chosen) >= capacity:
                break
        return chosen

    async def discover(self, state, item, payload):
        known = set(state["visited"]) | {p["url"] for p in state["pending"] + state["failed"]}
        grouped = {"internal": [], "external": []}
        for link in payload["links"]:
            category = "internal" if urlsplit(link["url"]).netloc in self.domains else "external"
            grouped[category].append({"href": link["url"], "text": link.get("text", "")})
        result = SimpleNamespace(links=grouped, metadata={})
        new, depths = [], {p["url"]: p["depth"] for p in state["pending"]}
        if self.kind == "dfs":
            self.policy._dfs_seen = known.copy()
        await self.policy.link_discovery(result, item["url"], item["depth"], known.copy(), new, depths)
        additions = []
        for url, parent in new:
            if url in known:
                continue
            if len(known) >= self.limit:
                state["coverage_limit"] = "discovery_budget"
                break
            score = self.scorer.score(url) if self.scorer else 0
            if score < self.threshold:
                continue
            additions.append({"url": url, "depth": depths[url], "parent_url": parent, "score": score})
            known.add(url)
        state["pending"] = additions + state["pending"] if self.kind == "dfs" else state["pending"] + additions
        if self.kind == "best_first":
            state["pending"].sort(key=lambda p: (-p.get("score", 0), p["depth"], p["url"]))
        elif self.kind == "bfs":
            state["pending"].sort(key=lambda p: p["depth"])
