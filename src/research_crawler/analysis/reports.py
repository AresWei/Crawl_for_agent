"""Deterministic statistics and safely rendered evidence reports; no model calls."""
from __future__ import annotations

from collections import Counter
from html import escape
import re

from .comments import LABELS, safe_url

REPORT_STYLE = """
body{font:15px/1.65 'Microsoft YaHei','Noto Sans CJK SC',sans-serif;color:#19392f;background:#fff;margin:48px auto;padding:0 28px;max-width:1040px}
h1{font-size:clamp(30px,4vw,46px);line-height:1.22;letter-spacing:-1px;margin:0 0 20px;max-width:860px}
h2{font-size:24px;margin:46px 0 18px}h3{font-size:17px;margin:0 0 12px}.summary{font-size:20px;margin:0 0 36px;color:#506d60}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:36px 56px}.dimension{break-inside:avoid}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{text-align:left;border-bottom:1px solid #dce5df;padding:7px 0}td:nth-child(n+2),th:nth-child(n+2){text-align:right}th{font-size:12px;font-weight:400;color:#63766d}
dl{display:grid;grid-template-columns:110px minmax(0,1fr);gap:5px 22px;font-size:12px;color:#5a6d62}dt,dd{margin:0;overflow-wrap:anywhere}
blockquote{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f1;padding:20px;margin:18px 0;color:#18291f;font-size:17px}
code{overflow-wrap:anywhere}.source{font-size:11px;color:#62746b;overflow-wrap:anywhere}.evidence{break-inside:avoid;border-top:1px solid #cfdbd3;padding-top:24px;margin-top:30px}
a{color:#286b4c}.limits{font-size:13px;color:#4b6155}.limits li{margin:8px 0}
@media(max-width:680px){body{margin:28px auto;padding:0 18px}.stats{grid-template-columns:1fr;gap:26px}dl{grid-template-columns:90px minmax(0,1fr);gap:8px 12px}blockquote{font-size:16px}}
@media print{body{margin:0;max-width:none;padding:0;font-size:11px}h1{font-size:30px}h2{break-after:avoid}.stats{gap:20px 30px}blockquote{font-size:12px}.source{font-size:9px}}
"""


def aggregate(annotations: list[dict]) -> dict:
    total = len(annotations)
    output = {"sample_size": total, "valid_annotations": sum(row["annotation_status"] == "valid" for row in annotations)}
    for name in ("emotion", "intent", "stance", "aspect"):
        counts = Counter()
        for row in annotations:
            labels = row["labels"]
            values = {item["label"] for item in labels["aspects"]} if name == "aspect" else {labels[name]["label"]}
            counts.update(values)
        output[name] = {label: {"count": counts[label], "percent_of_sample": round(100 * counts[label] / total, 2) if total else 0}
                        for label in LABELS[name]}
    output["aspect_percentages_may_exceed_100"] = True
    return output


def _md(value) -> str:
    text = escape(str(value), quote=False).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def report_document(annotations: list[dict], *, source_job_id: str, source_status: str,
                    coverage: dict, title: str) -> tuple[str, str, dict]:
    counts = aggregate(annotations)
    limits = [
        "本报告仅描述已选择、已保存的评论样本；不能代表全平台、全部观看者或总体意见。",
        "标签是本地模型或字面规则的解释性标注，不是经核实的事实、因果证据或人工真值。",
        "结构及引句通过校验不代表语义正确。未知计入分母；方面可多选，比例之和可超过100%。",
        "未提供明确立场对象时，立场全部为unknown；规则模式仅按字面线索匹配，不能处理反讽或上下文。",
    ]
    if not coverage.get("coverage_complete"):
        limits.append("采集或选择范围不完整；本次处理完成不等于全量覆盖。")
    methods = sorted({row["method"] + "/" + row["method_version"] for row in annotations})
    meta = [("样本数", counts["sample_size"]), ("有效标注", counts["valid_annotations"]),
            ("标注任务", source_job_id), ("任务状态", source_status), ("标注方法", "; ".join(methods)),
            ("原始采集任务", coverage.get("source_job_id", "unknown")),
            ("采集状态", coverage.get("source_collection_status", "unknown")),
            ("源评论数", coverage.get("available_comments", "unknown")),
            ("选入评论数", coverage.get("selected_comments", "unknown")),
            ("处理完成", str(bool(coverage.get("processing_complete")))),
            ("完整覆盖", str(bool(coverage.get("coverage_complete"))))]
    markdown = [f"# {_md(title)}", "", *[f"- {_md(k)}：{_md(v)}" for k, v in meta], "", "## 覆盖与解释限制", "",
                *[f"- {_md(item)}" for item in limits], "", "## 标签统计", ""]
    html = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'\">",
            f"<title>{escape(title)}</title><style>{REPORT_STYLE}</style></head><body>",
            f"<h1>{escape(title)}</h1><p class='summary'>{counts['sample_size']} 条已选评论 · {counts['valid_annotations']} 条有效标注 · {'完整覆盖' if coverage.get('coverage_complete') else '有限覆盖'}</p>",
            "<div class='stats'>"]
    for dimension in ("emotion", "intent", "stance", "aspect"):
        markdown += [f"### {dimension}", "", "| 标签 | 数量 | 样本占比 |", "|---|---:|---:|"]
        html += [f"<section class='dimension'><h3>{ {'emotion':'情绪','intent':'意图','stance':'立场','aspect':'方面'}[dimension] }</h3><table><thead><tr><th>标签</th><th>数量</th><th>样本占比</th></tr></thead><tbody>"]
        for label, item in counts[dimension].items():
            markdown.append(f"| {label} | {item['count']} | {item['percent_of_sample']:.2f}% |")
            html.append(f"<tr><td>{label}</td><td>{item['count']}</td><td>{item['percent_of_sample']:.2f}%</td></tr>")
        markdown.append("")
        html.append("</tbody></table></section>")
    markdown += ["## 原文与标注证据", ""]
    html += ["</div><h2>覆盖与解释限制</h2><ul class='limits'>", *[f"<li>{escape(item)}</li>" for item in limits],
             "</ul><h2>方法与溯源</h2><dl>", *[f"<dt>{escape(str(k))}</dt><dd>{escape(str(v))}</dd>" for k, v in meta],
             "</dl><h2>原文与标注证据</h2>"]
    for index, row in enumerate(annotations, 1):
        ref, labels = row["evidence"], row["labels"]
        line = "; ".join(f"{name}={labels[name]['label']}" for name in ("emotion", "intent", "stance"))
        line += "; aspects=" + ", ".join(item["label"] for item in labels["aspects"])
        quotes = "; ".join(f"{name}: {labels[name]['quote']}" for name in ("emotion", "intent", "stance") if labels[name]["quote"])
        quotes += "; " + "; ".join(f"aspect/{item['label']}: {item['quote']}" for item in labels["aspects"] if item["quote"])
        provenance = f"ID {ref['native_id']} · record {ref['record_key']} · hash {ref['content_hash']} · source date {ref.get('source_date') or 'unknown'} ({ref.get('source_date_field') or 'unknown'}) · observed {ref['observed_at']} · coverage {ref.get('source_coverage', {})}"
        status = f"{row['annotation_status']} / {row.get('unknown_reason') or 'none'}; model={row.get('model') or 'none'}; method={row['method']}/{row['method_version']}; label_version={row['label_version']}"
        markdown += [f"### [{index}] {_md(ref['native_id'])}", "", _md(line), "", _md(status), "", _md(provenance), "", "原文：", "",
                     *["    " + value for value in ref["text"].splitlines()], "", "引句：" + _md(quotes), ""]
        html += [f"<section class='evidence' id='e{index}'><h3>[{index}] {escape(ref['native_id'])}</h3><p>{escape(line)}</p>",
                 f"<p>{escape(status)}</p><p class='source'>{escape(provenance)}</p><blockquote>{escape(ref['text'])}</blockquote><p>引句：{escape(quotes)}</p>"]
        url = safe_url(ref["url"])
        if url:
            # Render escaped URL as text in Markdown: never interpolate into link syntax.
            markdown += ["来源：" + _md(url), ""]
            html.append(f"<a href='{escape(url, quote=True)}' rel='noreferrer noopener'>打开原文</a>")
        if ref.get("text_truncated") or ref.get("credentials_redacted"):
            note = f"原文展示限制：truncated={bool(ref.get('text_truncated'))}, redacted={bool(ref.get('credentials_redacted'))}"
            markdown += [_md(note), ""]
            html.append(f"<p>{escape(note)}</p>")
        html.append("</section>")
    html.append("</body></html>")
    return "\n".join(markdown), "".join(html), counts
