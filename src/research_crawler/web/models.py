from dataclasses import dataclass


@dataclass
class MarkdownGenerationResult:
    raw_markdown: str
    markdown_with_citations: str
    references_markdown: str
    fit_markdown: str = ""
    fit_html: str = ""
