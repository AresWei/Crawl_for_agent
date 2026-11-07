# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from typing import Dict, Optional


class AnchorElement:
    __slots__ = ["attrs", "count", "outcount"]

    def __init__(self, attrs: Dict[str, Optional[str]], count: int, outcount: int):
        self.attrs = attrs
        self.count = count
        self.outcount = outcount


class ListElement:
    __slots__ = ["name", "num"]

    def __init__(self, name: str, num: int):
        self.name = name
        self.num = num
