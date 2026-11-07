# Derived from Crawl4AI table_extraction.py, commit838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Apache-2.0; original license and attribution retained. Pure chunk/merge/validation methods.
from __future__ import annotations
from typing import List,Dict,Any,Optional

class TableAlgorithms:
    verbose = False

    def _create_chunk_html(self, header_html: str, body_rows: List[str], footer_html: Optional[str]) -> str:
            """
            Create a complete table HTML chunk with headers, body rows, and optional footer.
            """
            html_parts = ['<table>']
            
            if header_html:
                html_parts.append(header_html)
            
            html_parts.append('<tbody>')
            html_parts.extend(body_rows)
            html_parts.append('</tbody>')
            
            if footer_html:
                html_parts.append(footer_html)
            
            html_parts.append('</table>')
            
            return ''.join(html_parts)

    def _merge_chunk_results(self, chunk_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            """
            Merge results from multiple chunks into a single table.
            """
            # Sort by chunk index to maintain order
            chunk_results.sort(key=lambda x: x.get('chunk_index', 0))
            
            # Filter out failed chunks
            valid_chunks = [r for r in chunk_results if r.get('table')]
            
            if not valid_chunks:
                return []
            
            # Start with the first chunk's structure
            merged_table = valid_chunks[0]['table'].copy()
            
            # Concatenate rows from all chunks
            all_rows = []
            for chunk_result in valid_chunks:
                table = chunk_result['table']
                # Skip headers from non-first chunks (they're duplicates)
                rows = table.get('rows', [])
                all_rows.extend(rows)
            
            merged_table['rows'] = all_rows
            
            # Update metadata
            merged_table['metadata']['row_count'] = len(all_rows)
            merged_table['metadata']['chunked'] = True
            merged_table['metadata']['chunk_count'] = len(valid_chunks)
            
            if self.verbose:
                self._log("info", f"Merged {len(valid_chunks)} chunks into table with {len(all_rows)} rows")
            
            return [merged_table]

    def _validate_table_structure(self, table: Dict) -> bool:
            """
            Validate that the table has the required structure.
            
            Args:
                table: Table dictionary to validate
                
            Returns:
                True if valid, False otherwise
            """
            # Check required fields
            if not isinstance(table, dict):
                return False
            
            # Must have at least headers and rows
            if 'headers' not in table or 'rows' not in table:
                return False
            
            # Headers should be a list (but might be nested)
            headers = table.get('headers')
            if not isinstance(headers, list):
                return False
            
            # Flatten headers if nested
            while isinstance(headers, list) and len(headers) == 1 and isinstance(headers[0], list):
                table['headers'] = headers[0]
                headers = table['headers']
            
            # Rows should be a list
            rows = table.get('rows')
            if not isinstance(rows, list):
                return False
            
            # Flatten rows if deeply nested
            cleaned_rows = []
            for row in rows:
                # Handle multiple levels of nesting
                while isinstance(row, list) and len(row) == 1 and isinstance(row[0], list):
                    row = row[0]
                cleaned_rows.append(row)
            table['rows'] = cleaned_rows
            
            # Each row should be a list
            for row in table.get('rows', []):
                if not isinstance(row, list):
                    return False
            
            return True
