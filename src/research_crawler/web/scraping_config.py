# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
MIN_WORD_THRESHOLD = 1

IMAGE_DESCRIPTION_MIN_WORD_THRESHOLD = 1

IMPORTANT_ATTRS = ["src", "href", "alt", "title", "width", "height", "class", "id", "rowspan", "colspan"]

ONLY_TEXT_ELIGIBLE_TAGS = [
    "b",
    "i",
    "u",
    "span",
    "del",
    "ins",
    "sub",
    "sup",
    "strong",
    "em",
    "code",
    "kbd",
    "var",
    "s",
    "q",
    "abbr",
    "cite",
    "dfn",
    "time",
    "small",
    "mark",
]

SOCIAL_MEDIA_DOMAINS = [
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "instagram.com",
    "pinterest.com",
    "tiktok.com",
    "snapchat.com",
    "reddit.com",
]

IMAGE_SCORE_THRESHOLD = 2
