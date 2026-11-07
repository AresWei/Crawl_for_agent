"""Shared MediaCrawler author-grouping rules, preserving upstream behavior.

Copyright (c) 2025 relakkes@gmail.com.
Derived from tools/user_hash.py, NON-COMMERCIAL LEARNING LICENSE 1.1.
"""
import hashlib


def anonymize_user_id(value):
    if value is None or not str(value).strip():
        return ""
    return hashlib.sha256(str(value).strip().encode("utf-8")).hexdigest()[:16]


def mask_nickname(value):
    if value is None:
        return ""
    value = str(value)
    if len(value) <= 1:
        return "*"
    return value[0] + ("*" if len(value) == 2 else "***" + value[-1])
