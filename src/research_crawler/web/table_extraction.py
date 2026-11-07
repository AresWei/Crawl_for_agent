# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Union, Tuple
from lxml import etree
import re


class TableExtractionStrategy(ABC):
    """
    Abstract base class for all table extraction strategies.
    
    This class defines the interface that all table extraction strategies must implement.
    It provides a consistent way to detect and extract tables from HTML content.
    """
    
    def __init__(self, **kwargs):
        """
        Initialize the table extraction strategy.
        
        Args:
            **kwargs: Additional keyword arguments for specific strategies
        """
        self.verbose = kwargs.get("verbose", False)
        self.logger = kwargs.get("logger", None)
    
    @abstractmethod
    def extract_tables(self, element: etree.Element, **kwargs) -> List[Dict[str, Any]]:
        """
        Extract tables from the given HTML element.
        
        Args:
            element: The HTML element (typically the body or a container element)
            **kwargs: Additional parameters for extraction
            
        Returns:
            List of dictionaries containing table data, each with:
                - headers: List of column headers
                - rows: List of row data (each row is a list)
                - caption: Table caption if present
                - summary: Table summary attribute if present
                - metadata: Additional metadata about the table
        """
        pass
    
    def _log(self, level: str, message: str, tag: str = "TABLE", **kwargs):
        """Helper method to safely use logger."""
        if self.logger:
            log_method = getattr(self.logger, level, None)
            if log_method:
                log_method(message=message, tag=tag, **kwargs)


class DefaultTableExtraction(TableExtractionStrategy):
    """
    Default table extraction strategy that implements the current Crawl4AI table extraction logic.
    
    This strategy uses a scoring system to identify data tables (vs layout tables) and
    extracts structured data including headers, rows, captions, and summaries.
    It handles colspan and rowspan attributes to preserve table structure.
    """
    
    def __init__(self, **kwargs):
        """
        Initialize the default table extraction strategy.
        
        Args:
            table_score_threshold (int): Minimum score for a table to be considered a data table (default: 7)
            min_rows (int): Minimum number of rows for a valid table (default: 0)
            min_cols (int): Minimum number of columns for a valid table (default: 0)
            **kwargs: Additional parameters passed to parent class
        """
        super().__init__(**kwargs)
        self.table_score_threshold = kwargs.get("table_score_threshold", 7)
        self.min_rows = kwargs.get("min_rows", 0)
        self.min_cols = kwargs.get("min_cols", 0)
    
    def extract_tables(self, element: etree.Element, **kwargs) -> List[Dict[str, Any]]:
        """
        Extract all data tables from the HTML element.
        
        Args:
            element: The HTML element to search for tables
            **kwargs: Additional parameters (can override instance settings)
            
        Returns:
            List of dictionaries containing extracted table data
        """
        tables_data = []
        
        # Allow kwargs to override instance settings
        score_threshold = kwargs.get("table_score_threshold", self.table_score_threshold)
        
        # Find all table elements
        tables = element.xpath(".//table")
        
        for table in tables:
            # Check if this is a data table (not a layout table)
            if self.is_data_table(table, table_score_threshold=score_threshold):
                try:
                    table_data = self.extract_table_data(table)
                    
                    # Apply minimum size filters if specified
                    if self.min_rows > 0 and len(table_data.get("rows", [])) < self.min_rows:
                        continue
                    if self.min_cols > 0:
                        col_count = len(table_data.get("headers", [])) or (
                            max(len(row) for row in table_data.get("rows", [])) if table_data.get("rows") else 0
                        )
                        if col_count < self.min_cols:
                            continue
                    
                    tables_data.append(table_data)
                except Exception as e:
                    self._log("error", f"Error extracting table data: {str(e)}", "TABLE_EXTRACT")
                    continue
        
        return tables_data
    
    def is_data_table(self, table: etree.Element, **kwargs) -> bool:
        """
        Determine if a table is a data table (vs. layout table) using a scoring system.
        
        Args:
            table: The table element to evaluate
            **kwargs: Additional parameters (e.g., table_score_threshold)
            
        Returns:
            True if the table scores above the threshold, False otherwise
        """
        score = 0
        
        # Check for thead and tbody
        has_thead = len(table.xpath(".//thead")) > 0
        has_tbody = len(table.xpath(".//tbody")) > 0
        if has_thead:
            score += 2
        if has_tbody:
            score += 1
        
        # Check for th elements
        th_count = len(table.xpath(".//th"))
        if th_count > 0:
            score += 2
            if has_thead or table.xpath(".//tr[1]/th"):
                score += 1
        
        # Check for nested tables (negative indicator)
        if len(table.xpath(".//table")) > 0:
            score -= 3
        
        # Role attribute check
        role = table.get("role", "").lower()
        if role in {"presentation", "none"}:
            score -= 3
        
        # Column consistency
        rows = table.xpath(".//tr")
        if not rows:
            return False
        
        col_counts = [len(row.xpath(".//td|.//th")) for row in rows]
        if col_counts:
            avg_cols = sum(col_counts) / len(col_counts)
            variance = sum((c - avg_cols)**2 for c in col_counts) / len(col_counts)
            if variance < 1:
                score += 2
        
        # Caption and summary
        if table.xpath(".//caption"):
            score += 2
        if table.get("summary"):
            score += 1
        
        # Text density
        total_text = sum(
            len(''.join(cell.itertext()).strip()) 
            for row in rows 
            for cell in row.xpath(".//td|.//th")
        )
        total_tags = sum(1 for _ in table.iterdescendants())
        text_ratio = total_text / (total_tags + 1e-5)
        if text_ratio > 20:
            score += 3
        elif text_ratio > 10:
            score += 2
        
        # Data attributes
        data_attrs = sum(1 for attr in table.attrib if attr.startswith('data-'))
        score += data_attrs * 0.5
        
        # Size check
        if col_counts and len(rows) >= 2:
            avg_cols = sum(col_counts) / len(col_counts)
            if avg_cols >= 2:
                score += 2
        
        threshold = kwargs.get("table_score_threshold", self.table_score_threshold)
        return score >= threshold
    
    # HTML Standard caps colspan at 1000; browsers clamp to the same value.
    COLSPAN_LIMIT = 1000

    @classmethod
    def _build_grid(cls, rows: List[etree.Element]) -> List[List[str]]:
        """Lay <tr> cells into a rectangular grid, honouring colspan/rowspan.

        Each cell writes its text into every slot of the rectangle it spans,
        including slots in later rows, so a row only has to fill the slots the
        rows above it left free. Nothing is carried between iterations, so a
        short row cannot leave a stale span behind (see issue #2258).
        """
        grid: List[List[str]] = [[] for _ in rows]

        def span(value, limit: int) -> int:
            """Read a span attribute the way a browser does: junk counts as 1."""
            try:
                return min(max(int(value), 1), limit)
            except (TypeError, ValueError):
                return 1

        def put(r: int, c: int, text: str) -> None:
            row = grid[r]
            row.extend([None] * (c + 1 - len(row)))
            row[c] = text

        for r, tr in enumerate(rows):
            c = 0
            for cell in tr.xpath("./th|./td"):
                while c < len(grid[r]) and grid[r][c] is not None:
                    c += 1  # slot already taken by a rowspan from above
                text = cell.text_content().strip()
                # Both spans are clamped, so one crafted cell cannot make the
                # work explode: scraped HTML is untrusted, and rowspan*colspan
                # is a product. COLSPAN_LIMIT is the HTML Standard's cap, which
                # is what browsers apply; a rowspan cannot reach past the last
                # row, which is a tighter bound than the standard's 65534.
                colspan = span(cell.get("colspan"), cls.COLSPAN_LIMIT)
                rowspan = span(cell.get("rowspan"), len(grid) - r)
                for dr in range(rowspan):
                    for dc in range(colspan):
                        put(r + dr, c + dc, text)
                c += colspan

        return [[text or "" for text in row] for row in grid if row]

    def extract_table_data(self, table: etree.Element) -> Dict[str, Any]:
        """
        Extract structured data from a table element.
        
        Args:
            table: The table element to extract data from
            
        Returns:
            Dictionary containing:
                - headers: List of column headers
                - rows: List of row data (each row is a list)
                - caption: Table caption if present
                - summary: Table summary attribute if present
                - metadata: Additional metadata about the table
        """
        # Extract caption and summary
        caption = table.xpath(".//caption/text()")
        caption = caption[0].strip() if caption else ""
        summary = table.get("summary", "").strip()
        
        # Extract headers with colspan handling
        headers = []
        implicit_header_row = None
        thead_rows = table.xpath(".//thead/tr")
        if thead_rows:
            header_cells = thead_rows[0].xpath(".//th")
            for cell in header_cells:
                text = cell.text_content().strip()
                colspan = int(cell.get("colspan", 1))
                headers.extend([text] * colspan)
        else:
            # Check first row for headers
            first_row = table.xpath(".//tr[1]")
            if first_row:
                implicit_header_row = first_row[0]
                for cell in implicit_header_row.xpath(".//th|.//td"):
                    text = cell.text_content().strip()
                    colspan = int(cell.get("colspan", 1))
                    headers.extend([text] * colspan)
        
        # Extract rows, laying each cell into a grid so colspan/rowspan land in
        # the columns they actually cover and <th> row headers are kept (#2258).
        body_rows = table.xpath(".//tr[not(ancestor::thead)]")
        # A first row made only of <th> is the header, so don't repeat it as
        # data. A first row mixing <th scope="row"> with <td> is a key/value
        # data row and must stay.
        if implicit_header_row is not None and not implicit_header_row.xpath("./td"):
            body_rows = body_rows[1:]
        rows = self._build_grid(body_rows)
        
        # Align rows with headers
        max_columns = len(headers) if headers else (
            max(len(row) for row in rows) if rows else 0
        )
        aligned_rows = []
        for row in rows:
            aligned = row[:max_columns] + [''] * (max_columns - len(row))
            aligned_rows.append(aligned)
        
        # Generate default headers if none found
        if not headers and max_columns > 0:
            headers = [f"Column {i+1}" for i in range(max_columns)]
        
        # Build metadata
        metadata = {
            "row_count": len(aligned_rows),
            "column_count": max_columns,
            "has_headers": bool(thead_rows) or bool(table.xpath(".//tr[1]/th")),
            "has_caption": bool(caption),
            "has_summary": bool(summary)
        }
        
        # Add table attributes that might be useful
        if table.get("id"):
            metadata["id"] = table.get("id")
        if table.get("class"):
            metadata["class"] = table.get("class")
        
        return {
            "headers": headers,
            "rows": aligned_rows,
            "caption": caption,
            "summary": summary,
            "metadata": metadata
        }


class NoTableExtraction(TableExtractionStrategy):
    """
    A strategy that does not extract any tables.
    
    This can be used to explicitly disable table extraction when needed.
    """
    
    def extract_tables(self, element: etree.Element, **kwargs) -> List[Dict[str, Any]]:
        """
        Return an empty list (no tables extracted).
        
        Args:
            element: The HTML element (ignored)
            **kwargs: Additional parameters (ignored)
            
        Returns:
            Empty list
        """
        return []
