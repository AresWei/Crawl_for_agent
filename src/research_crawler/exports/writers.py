"""Atomic local formats; the companion JSONL is the lossless interchange form."""
from __future__ import annotations

import csv
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import zipfile

from ..core.records import canonical_json


def artifact(path: Path, media_type: str) -> dict:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"path": str(path.resolve()), "bytes": path.stat().st_size,
            "sha256": digest, "content_type": media_type}


def atomic(path: Path, writer) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        writer(temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def rows(snapshot: Path):
    with snapshot.open(encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def tables(snapshot: Path) -> dict:
    """Union headers by source/kind. Never derive later row order from its keys."""
    groups = {}
    for row in rows(snapshot):
        group = groups.setdefault((row["source"], row["kind"]), {"columns": [], "count": 0})
        group["count"] += 1
        for key in row["payload"]:
            if key not in group["columns"]:
                group["columns"].append(key)
    output = {}
    for index, ((source, kind), group) in enumerate(groups.items(), 1):
        name = f"t{index}_" + re.sub(r"[^a-zA-Z0-9_]", "_", f"{source}_{kind}")[:35]
        prefix = "_rc_"
        while any(key.startswith(prefix) for key in group["columns"]):
            prefix = "_" + prefix
        output[name] = {**group, "source": source, "kind": kind, "metadata_prefix": prefix}
    return output


META = ("job_id", "record_key", "content_hash", "native_id", "url", "source_date",
        "source_date_field", "observed_at", "coverage")


def flat(row, table):
    return {**{key: row["payload"].get(key) for key in table["columns"]},
            **{table["metadata_prefix"] + key: row.get(key) for key in META}}


def headers(table):
    return table["columns"] + [table["metadata_prefix"] + key for key in META]


def cell(value):
    return canonical_json(value) if isinstance(value, (dict, list)) else value


def csv_cell(value):
    value = cell(value)
    # CSV is a presentation format; exact strings remain in companion JSONL.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_json(path, snapshot, manifest):
    def write(temp):
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"manifest":' + canonical_json(manifest) + ',"records":[')
            for index, row in enumerate(rows(snapshot)):
                handle.write(("," if index else "") + canonical_json(row))
            handle.write("]}\n")
    atomic(path, write)


def write_csv(path, snapshot, manifest):
    # CSV tables plus provenance manifest are one atomic downloadable archive.
    def write(temp):
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical_json(manifest))
            import io
            for name, table in manifest["tables"].items():
                with archive.open(name + ".csv", "w") as stream:
                    with io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerow([csv_cell(key) for key in headers(table)])
                        for row in rows(snapshot):
                            if (row["source"], row["kind"]) == (table["source"], table["kind"]):
                                writer.writerow([csv_cell(value) for value in flat(row, table).values()])
    atomic(path, write)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def write_sqlite(path, snapshot, manifest):
    def write(temp):
        temp.unlink(missing_ok=True)
        with closing(sqlite3.connect(temp)) as db, db:
            db.execute("CREATE TABLE export_manifest (manifest_json TEXT NOT NULL)")
            db.execute("INSERT INTO export_manifest VALUES (?)", (canonical_json(manifest),))
            db.execute("CREATE TABLE record_envelopes (record_key TEXT, content_hash TEXT, envelope_json TEXT NOT NULL, PRIMARY KEY(record_key,content_hash))")
            for name, table in manifest["tables"].items():
                # Case-insensitive duplicate column identifiers are legal JSON keys
                # but illegal SQL columns. Fail explicitly rather than lose a field.
                keys = headers(table)
                if len({key.casefold() for key in keys}) != len(keys):
                    raise ValueError("SQLite cannot represent case-insensitive duplicate payload columns")
                db.execute(f"CREATE TABLE {quote(name)} (" + ",".join(quote(key) for key in keys) + ")")
            by_group = {(t["source"], t["kind"]): (name, t) for name, t in manifest["tables"].items()}
            for row in rows(snapshot):
                db.execute("INSERT INTO record_envelopes VALUES (?,?,?)", (row["record_key"], row["content_hash"], canonical_json(row)))
                name, table = by_group[row["source"], row["kind"]]
                values = [cell(value) for value in flat(row, table).values()]
                # SQLite integer range is narrower than JSON's arbitrary precision.
                values = [str(v) if isinstance(v, int) and not -(2**63) <= v < 2**63 else v for v in values]
                db.execute(f"INSERT INTO {quote(name)} VALUES (" + ",".join("?" for _ in values) + ")", values)
    atomic(path, write)


def write_excel(path, snapshot, manifest):
    # openpyxl's formatted workbook holds cells in RAM. Keep this optional
    # presentation bounded; JSON/JSONL/CSV/SQLite continue streaming.
    if sum((t["count"] + 1) * len(headers(t)) for t in manifest["tables"].values()) > 250000:
        raise ValueError("Formatted Excel exceeds 250000 cells; use JSONL/CSV/SQLite or a smaller selection")
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from .excel_styles import ExcelStyles

    def write(temp):
        workbook, styles = Workbook(), ExcelStyles()
        overview = workbook.active
        overview.title = "Manifest"
        overview.append(["Lossless source", "records.snapshot.jsonl"])
        overview.append(["Snapshot SHA256", manifest["snapshot_sha256"]])
        overview.append(["Source job", manifest["source_job_id"]])
        overview.append(["Source status", manifest["source_status"]])
        overview.append(["Cell handling", "JSON for nested values; long/illegal XML text has Overflow chunks; strings are never formulas"])
        overflow = workbook.create_sheet("Overflow")
        styles._write_headers(overflow, ["table", "record_key", "content_hash", "column", "chunk", "json_string_chunk"])
        by_group = {}
        for index, (name, table) in enumerate(manifest["tables"].items(), 1):
            category = {"comment": "Comments", "creator": "Creators", "contact": "Contacts", "dynamic": "Dynamics"}.get(table["kind"], "Contents")
            sheet = workbook.create_sheet(f"{index}_{table['source']}_{category}"[:31])
            styles._write_headers(sheet, headers(table))
            by_group[table["source"], table["kind"]] = (name, table, sheet)
        for row in rows(snapshot):
            name, table, sheet = by_group[row["source"], row["kind"]]
            values = {key: cell(value) for key, value in flat(row, table).items()}
            for key, value in list(values.items()):
                if isinstance(value, int) and (abs(value) > 2**53):
                    values[key] = str(value)
                if isinstance(value, str) and (len(value) > 30000 or ILLEGAL_CHARACTERS_RE.search(value)):
                    encoded = json.dumps(value, ensure_ascii=True)
                    for index in range(0, len(encoded), 30000):
                        overflow.append([name, row["record_key"], row["content_hash"], key, index // 30000, encoded[index:index + 30000]])
                        overflow.cell(overflow.max_row, 6).data_type = "s"
                    values[key] = "[Full value in Overflow sheet]"
            styles._write_row(sheet, values, headers(table))
            for item in sheet[sheet.max_row]:
                if isinstance(item.value, str):
                    item.data_type = "s"
        if overflow.max_row == 1:
            workbook.remove(overflow)
        for sheet in workbook:
            styles._auto_adjust_column_width(sheet)
            sheet.freeze_panes = "A2"
            for item in sheet[1]:
                if isinstance(item.value, str):
                    item.data_type = "s"
        workbook.save(temp)
        workbook.close()
    atomic(path, write)


WRITERS = {"json": (write_json, "records.json", "application/json"),
           "csv": (write_csv, "tables.csv.zip", "application/zip"),
           "sqlite": (write_sqlite, "records.sqlite3", "application/vnd.sqlite3"),
           "excel": (write_excel, "records.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
