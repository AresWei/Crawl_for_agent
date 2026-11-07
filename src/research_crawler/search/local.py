from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..core.jobs import JobContext
from ..core.records import Record, canonical_json
from ..core.store import Store


TEXT_FIELDS = ("title", "markdown", "text", "content", "description", "desc")
DERIVED_SOURCE = "local_search"
SOURCE_FILTER_SQL = "r.source NOT IN (?, ?, ?, ?, ?, ?, ?) AND r.kind != ?"
SOURCE_FILTER_ARGS = (DERIVED_SOURCE, "local_model", "analysis", "web_search", "comment_analysis",
                      "derived_exports", "local_creation", "visual_claim")


def _parameters(query: Any, limit: Any) -> tuple[str, int]:
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValueError("query must contain 1 to 300 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    return query.strip(), limit


class LocalSearch:
    """A derived index of the latest observed version of each collected record.

    The corpus is the existing Store only. No search-engine, model, or network
    calls occur. Index tables can be rebuilt without changing source evidence.
    """

    def __init__(self, store: Store):
        self.store = store

    def _refresh(self, db) -> dict:
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS search_local_fts USING fts5(
            record_key UNINDEXED, content_hash UNINDEXED, fields_json UNINDEXED,
            title, body, tokenize='unicode61')""")
        # Hold one transaction so concurrent refreshes cannot duplicate documents.
        db.execute("BEGIN IMMEDIATE")
        existing = {row["record_key"]: row["content_hash"] for row in db.execute(
            "SELECT record_key,content_hash FROM search_local_fts")}
        rows = db.execute(f"""SELECT * FROM (
            SELECT v.record_key,v.content_hash,v.payload_json,
                ROW_NUMBER() OVER (PARTITION BY v.record_key
                    ORDER BY v.last_seen DESC,v.rowid DESC) AS position
            FROM record_versions v JOIN records r ON r.key=v.record_key
            WHERE {SOURCE_FILTER_SQL}
        ) WHERE position=1""", SOURCE_FILTER_ARGS)
        indexed = changed = 0
        keep = set()
        for row in rows:
            payload = json.loads(row["payload_json"])
            # A newer captured challenge must remove any older indexed version,
            # while the original observation remains available for inspection.
            if payload.get('source_usable') is False or payload.get('blocking',{}).get('exclude_from_answers') is True:
                continue
            fields = {name: payload[name] for name in TEXT_FIELDS
                      if isinstance(payload.get(name), str) and payload[name].strip()}
            if not fields:
                continue
            key, digest = row["record_key"], row["content_hash"]
            keep.add(key)
            indexed += 1
            if existing.get(key) == digest:
                continue
            db.execute("DELETE FROM search_local_fts WHERE record_key=?", (key,))
            db.execute("INSERT INTO search_local_fts VALUES(?,?,?,?,?)", (
                key, digest, canonical_json(fields), fields.get("title", ""),
                "\n".join(value for name, value in fields.items() if name != "title")))
            changed += 1
        for key in existing.keys() - keep:
            db.execute("DELETE FROM search_local_fts WHERE record_key=?", (key,))
            changed += 1
        return {"indexed_records": indexed, "updated_records": changed,
                "version_policy": "latest_observed", "fields": list(TEXT_FIELDS)}

    @staticmethod
    def _evidence(db, row, terms: list[str], method: str) -> dict:
        fields = json.loads(row["fields_json"])
        # Preserve the exact source field and right-exclusive character offsets.
        candidates = [(name, value) for name, value in fields.items()
                      if any(term.lower() in value.lower() for term in terms)]
        candidates.sort(key=lambda pair: pair[0] == "title")
        name, value = next(iter(candidates or fields.items()))
        positions = [value.lower().find(term.lower()) for term in terms]
        first = min((position for position in positions if position >= 0), default=0)
        start = max(0, first - 120)
        end = min(len(value), start + 480)
        source = db.execute("""SELECT r.source,r.kind,r.native_id,
                COALESCE(o.url,r.url) AS url,v.source_date,v.source_date_field,
                o.observed_at,o.coverage_json,o.job_id AS source_job_id
            FROM records r JOIN record_versions v ON v.record_key=r.key
            LEFT JOIN observations o ON o.rowid=(
                SELECT o2.rowid FROM observations o2
                WHERE o2.record_key=v.record_key AND o2.content_hash=v.content_hash
                ORDER BY o2.observed_at DESC,o2.rowid DESC LIMIT 1)
            WHERE r.key=? AND v.content_hash=?""",
            (row["record_key"], row["content_hash"])).fetchone()
        result = dict(source)
        coverage_json = result.pop("coverage_json")
        result.update({
            "reference": f"record:{row['record_key']}@{row['content_hash']}",
            "record_key": row["record_key"], "content_hash": row["content_hash"],
            "title": fields.get("title", ""), "coverage": json.loads(coverage_json) if coverage_json else {},
            "excerpt": value[start:end], "excerpt_field": name,
            "excerpt_range": {"start": start, "end": end, "unit": "unicode_code_points", "end_exclusive": True},
            "retrieval_method": method,
        })
        return result

    def search(self, query: str, limit: int = 10) -> dict:
        query, limit = _parameters(query, limit)
        terms = query.split()
        # Treat the entire user query as literal terms, never as FTS operators.
        fts_query = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
        with self.store.connection() as db:
            index = self._refresh(db)
            rows = list(db.execute("""SELECT * FROM search_local_fts
                WHERE search_local_fts MATCH ? ORDER BY bm25(search_local_fts),record_key LIMIT ?""",
                (fts_query, limit))) if re.search(r"\w", query) else []
            matches = [(row, "fts5") for row in rows]
            # unicode61 does not split continuous Chinese into words. Literal
            # matching also covers short Chinese queries, without LIKE wildcards.
            if len(matches) < limit:
                predicates = " AND ".join(
                    "instr(lower(title || char(10) || body),lower(?)) > 0" for _ in terms)
                exclusions = ""
                params: list[Any] = list(terms)
                if rows:
                    exclusions = " AND record_key NOT IN (" + ",".join("?" for _ in rows) + ")"
                    params.extend(row["record_key"] for row in rows)
                params.append(limit - len(matches))
                fallback = db.execute("SELECT * FROM search_local_fts WHERE " + predicates
                    + exclusions + " ORDER BY record_key LIMIT ?", params)
                matches.extend((row, "literal_substring") for row in fallback)
            evidence = [self._evidence(db, row, terms, method) for row, method in matches]
        return {"query": query, "mode": "local_corpus_search", "network_used": False,
                "model_used": False, "index": index, "results": evidence,
                "returned": len(evidence), "limit": limit}


def extractive_answer(search: dict) -> dict:
    evidence = search["results"]
    return {"query": search["query"], "mode": "extractive_evidence", "model_used": False,
            "network_used": False, "answer_status": "evidence_found" if evidence else "no_evidence",
            "label": "来源原文摘录（未使用生成式模型，不代表已回答或验证问题）",
            "answer": "\n\n".join(f"[{i}] {item['excerpt']}" for i, item in enumerate(evidence, 1)) if evidence else None,
            "message": None if evidence else "本地已采集文本中未找到匹配证据；无法据此回答。",
            "evidence": [dict(item, citation=i) for i, item in enumerate(evidence, 1)],
            "index": search["index"]}


async def _run(context: JobContext, *, answer: bool) -> dict:
    query, limit = _parameters(context.params.get("query"), context.params.get("limit", 5 if answer else 10))
    stream = "local_answer" if answer else "local_search"
    checkpoint = await context.checkpoint(stream)
    if checkpoint and checkpoint["exhausted"]:
        return checkpoint["metadata"]["result"]
    found = await asyncio.to_thread(LocalSearch(context.store).search, query, limit)
    await context.check_control()
    result = extractive_answer(found) if answer else found
    record = Record(source=DERIVED_SOURCE, kind="evidence_answer" if answer else "search_result",
                    native_id=context.job_id, url="", payload=result,
                    coverage={"scope": "local_index", "generated": False,
                              "source_count": len(found["results"]), "query": query})
    await context.commit_page(stream, [record], next_cursor=None, exhausted=True,
                              expected_revision=checkpoint["revision"] if checkpoint else 0,
                              metadata={"result": result})
    return result


async def search_local(context: JobContext) -> dict:
    return await _run(context, answer=False)


async def answer_local(context: JobContext) -> dict:
    return await _run(context, answer=True)
