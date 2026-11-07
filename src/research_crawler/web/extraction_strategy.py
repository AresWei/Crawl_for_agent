# Derived from Crawl4AI 0.9.5, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
# Copyright Crawl4AI contributors. See LICENSE.crawl4ai and source-manifest.json.
# Local modifications: source selection and application-local dependency imports.
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, List, Dict, Optional, Tuple, Pattern, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import IntFlag, auto
from bs4 import BeautifulSoup
from lxml import html, etree
import re, json


class ExtractionStrategy(ABC):
    """
    Abstract base class for all extraction strategies.
    """

    def __init__(self, input_format: str = "markdown", **kwargs):
        """
        Initialize the extraction strategy.

        Args:
            input_format: Content format to use for extraction.
                         Options: "markdown" (default), "html", "fit_markdown"
            **kwargs: Additional keyword arguments
        """
        self.input_format = input_format
        self.DEL = "<|DEL|>"
        self.name = self.__class__.__name__
        self.verbose = kwargs.get("verbose", False)

    @abstractmethod
    def extract(self, url: str, html: str, *q, **kwargs) -> List[Dict[str, Any]]:
        """
        Extract meaningful blocks or chunks from the given HTML.

        :param url: The URL of the webpage.
        :param html: The HTML content of the webpage.
        :return: A list of extracted blocks or chunks.
        """
        pass

    def run(self, url: str, sections: List[str], *q, **kwargs) -> List[Dict[str, Any]]:
        """
        Process sections of text in parallel by default.

        :param url: The URL of the webpage.
        :param sections: List of sections (strings) to process.
        :return: A list of processed JSON blocks.
        """
        extracted_content = []
        with ThreadPoolExecutor() as executor:
            futures = [
                executor.submit(self.extract, url, section, **kwargs)
                for section in sections
            ]
            for future in as_completed(futures):
                extracted_content.extend(future.result())
        return extracted_content

    async def arun(self, url: str, sections: List[str], *q, **kwargs) -> List[Dict[str, Any]]:
        """
        Async version: Process sections of text in parallel using asyncio.

        Default implementation runs the sync version in a thread pool.
        Subclasses can override this for true async processing.

        :param url: The URL of the webpage.
        :param sections: List of sections (strings) to process.
        :return: A list of processed JSON blocks.
        """
        import asyncio
        return await asyncio.to_thread(self.run, url, sections, *q, **kwargs)


class NoExtractionStrategy(ExtractionStrategy):
    """
    A strategy that does not extract any meaningful content from the HTML. It simply returns the entire HTML as a single block.
    """

    def extract(self, url: str, html: str, *q, **kwargs) -> List[Dict[str, Any]]:
        """
        Extract meaningful blocks or chunks from the given HTML.
        """
        return [{"index": 0, "content": html}]

    def run(self, url: str, sections: List[str], *q, **kwargs) -> List[Dict[str, Any]]:
        return [
            {"index": i, "tags": [], "content": section}
            for i, section in enumerate(sections)
        ]


class JsonElementExtractionStrategy(ExtractionStrategy):
    """
    Abstract base class for extracting structured JSON from HTML content.

    How it works:
    1. Parses HTML content using the `_parse_html` method.
    2. Uses a schema to define base selectors, fields, and transformations.
    3. Extracts data hierarchically, supporting nested fields and lists.
    4. Handles computed fields with expressions or functions.

    Attributes:
        DEL (str): Delimiter used to combine HTML sections. Defaults to '\n'.
        schema (Dict[str, Any]): The schema defining the extraction rules.
        verbose (bool): Enables verbose logging for debugging purposes.

    Methods:
        extract(url, html_content, *q, **kwargs): Extracts structured data from HTML content.
        _extract_item(element, fields): Extracts fields from a single element.
        _extract_single_field(element, field): Extracts a single field based on its type.
        _apply_transform(value, transform): Applies a transformation to a value.
        _compute_field(item, field): Computes a field value using an expression or function.
        run(url, sections, *q, **kwargs): Combines HTML sections and runs the extraction strategy.

    Abstract Methods:
        _parse_html(html_content): Parses raw HTML into a structured format (e.g., BeautifulSoup or lxml).
        _get_base_elements(parsed_html, selector): Retrieves base elements using a selector.
        _get_elements(element, selector): Retrieves child elements using a selector.
        _get_element_text(element): Extracts text content from an element.
        _get_element_html(element): Extracts raw HTML from an element.
        _get_element_attribute(element, attribute): Extracts an attribute's value from an element.
    """

    DEL = "\n"

    def __init__(self, schema: Dict[str, Any], **kwargs):
        """
        Initialize the JSON element extraction strategy with a schema.

        Args:
            schema (Dict[str, Any]): The schema defining the extraction rules.
        """
        super().__init__(**kwargs)
        self.schema = schema
        self.verbose = kwargs.get("verbose", False)

    def extract(
        self, url: str, html_content: str, *q, **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Extract structured data from HTML content.

        How it works:
        1. Parses the HTML content using the `_parse_html` method.
        2. Identifies base elements using the schema's base selector.
        3. Extracts fields from each base element using `_extract_item`.

        Args:
            url (str): The URL of the page being processed.
            html_content (str): The raw HTML content to parse and extract.
            *q: Additional positional arguments.
            **kwargs: Additional keyword arguments for custom extraction.

        Returns:
            List[Dict[str, Any]]: A list of extracted items, each represented as a dictionary.
        """

        parsed_html = self._parse_html(html_content)
        base_elements = self._get_base_elements(
            parsed_html, self.schema["baseSelector"]
        )

        results = []
        for element in base_elements:
            # Extract base element attributes
            item = {}
            if "baseFields" in self.schema:
                for field in self.schema["baseFields"]:
                    value = self._extract_single_field(element, field)
                    if value is not None:
                        item[field["name"]] = value

            # Extract child fields
            field_data = self._extract_item(element, self.schema["fields"])
            item.update(field_data)

            if item:
                results.append(item)

        return results

    @abstractmethod
    def _parse_html(self, html_content: str):
        """Parse HTML content into appropriate format"""
        pass

    @abstractmethod
    def _get_base_elements(self, parsed_html, selector: str):
        """Get all base elements using the selector"""
        pass

    @abstractmethod
    def _get_elements(self, element, selector: str):
        """Get child elements using the selector"""
        pass

    def _extract_field(self, element, field):
        try:
            if "source" in field:
                element = self._resolve_source(element, field["source"])
                if element is None:
                    return field.get("default")

            if field["type"] == "nested":
                nested_elements = self._get_elements(element, field["selector"])
                nested_element = nested_elements[0] if nested_elements else None
                return (
                    self._extract_item(nested_element, field["fields"])
                    if nested_element
                    else {}
                )

            if field["type"] == "list":
                elements = self._get_elements(element, field["selector"])
                return [self._extract_list_item(el, field["fields"]) for el in elements]

            if field["type"] == "nested_list":
                elements = self._get_elements(element, field["selector"])
                return [self._extract_item(el, field["fields"]) for el in elements]

            return self._extract_single_field(element, field)
        except Exception as e:
            if self.verbose:
                print(f"Error extracting field {field['name']}: {str(e)}")
            return field.get("default")

    def _extract_single_field(self, element, field):
        """
        Extract a single field based on its type.

        How it works:
        1. Selects the target element using the field's selector.
        2. Extracts the field value based on its type (e.g., text, attribute, regex).
        3. Applies transformations if defined in the schema.

        Args:
            element: The base element to extract the field from.
            field (Dict[str, Any]): The field definition in the schema.

        Returns:
            Any: The extracted field value.
        """

        if "selector" in field:
            selected = self._get_elements(element, field["selector"])
            if not selected:
                return field.get("default")
            selected = selected[0]
        else:
            selected = element

        type_pipeline = field["type"]
        if not isinstance(type_pipeline, list):
            type_pipeline = [type_pipeline]
        value = selected
        for step in type_pipeline:
            if step == "text":
                value = self._get_element_text(value)
            elif step == "attribute":
                value = self._get_element_attribute(value, field["attribute"])
            elif step == "html":
                value = self._get_element_html(value)
            elif step == "regex":
                pattern = field.get("pattern")
                if pattern:
                    # If value is still an element, extract text first (backward compat)
                    if not isinstance(value, str):
                        value = self._get_element_text(value)
                    if isinstance(value, str):
                        match = re.search(pattern, value)
                        value = match.group(field.get("group", 1)) if match else None
                    else:
                        value = None
            if value is None:
                break

        if "transform" in field:
            value = self._apply_transform(value, field["transform"])

        return value if value is not None else field.get("default")

    def _extract_list_item(self, element, fields):
        item = {}
        for field in fields:
            value = self._extract_single_field(element, field)
            if value is not None:
                item[field["name"]] = value
        return item

    def _extract_item(self, element, fields):
        """
        Extracts fields from a given element.

        How it works:
        1. Iterates through the fields defined in the schema.
        2. Handles computed, single, and nested field types.
        3. Updates the item dictionary with extracted field values.

        Args:
            element: The base element to extract fields from.
            fields (List[Dict[str, Any]]): The list of fields to extract.

        Returns:
            Dict[str, Any]: A dictionary representing the extracted item.
        """

        item = {}
        for field in fields:
            if field["type"] == "computed":
                value = self._compute_field(item, field)
            else:
                value = self._extract_field(element, field)
            if value is not None:
                item[field["name"]] = value
        return item

    def _apply_transform(self, value, transform):
        """
        Apply a transformation to a value.

        How it works:
        1. Checks the transformation type (e.g., `lowercase`, `strip`).
        2. Applies the transformation to the value.
        3. Returns the transformed value.

        Args:
            value (str): The value to transform.
            transform (str): The type of transformation to apply.

        Returns:
            str: The transformed value.
        """

        if transform == "lowercase":
            return value.lower()
        elif transform == "uppercase":
            return value.upper()
        elif transform == "strip":
            return value.strip()
        return value

    def _compute_field(self, item, field):
        try:
            if "expression" in field:
                import logging
                logging.getLogger(__name__).warning(
                    "Computed field 'expression' is disabled for security "
                    "(eval on untrusted input). Use 'function' key with a "
                    "Python callable instead."
                )
                return field.get("default")
            elif "function" in field:
                return field["function"](item)
        except Exception as e:
            if self.verbose:
                print(f"Error computing field {field['name']}: {str(e)}")
            return field.get("default")

    def run(self, url: str, sections: List[str], *q, **kwargs) -> List[Dict[str, Any]]:
        """
        Run the extraction strategy on a combined HTML content.

        How it works:
        1. Combines multiple HTML sections using the `DEL` delimiter.
        2. Calls the `extract` method with the combined HTML.

        Args:
            url (str): The URL of the page being processed.
            sections (List[str]): A list of HTML sections.
            *q: Additional positional arguments.
            **kwargs: Additional keyword arguments for custom extraction.

        Returns:
            List[Dict[str, Any]]: A list of extracted items.
        """

        combined_html = self.DEL.join(sections)
        return self.extract(url, combined_html, **kwargs)

    @abstractmethod
    def _get_element_text(self, element) -> str:
        """Get text content from element"""
        pass

    @abstractmethod
    def _get_element_html(self, element) -> str:
        """Get HTML content from element"""
        pass

    @abstractmethod
    def _get_element_attribute(self, element, attribute: str):
        """Get attribute value from element"""
        pass

    @abstractmethod
    def _resolve_source(self, element, source: str):
        """Navigate to a sibling element relative to the base element.

        Used when a field's data lives in a sibling of the base element
        rather than a descendant. For example, Hacker News splits each
        submission across two sibling <tr> rows.

        Args:
            element: The current base element.
            source: A sibling selector string. Currently supports the
                ``"+ <selector>"`` syntax which navigates to the next
                sibling matching ``<selector>``.

        Returns:
            The resolved sibling element, or ``None`` if not found.
        """
        pass



class JsonCssExtractionStrategy(JsonElementExtractionStrategy):
    """
    Concrete implementation of `JsonElementExtractionStrategy` using CSS selectors.

    How it works:
    1. Parses HTML content with BeautifulSoup.
    2. Selects elements using CSS selectors defined in the schema.
    3. Extracts field data and applies transformations as defined.

    Attributes:
        schema (Dict[str, Any]): The schema defining the extraction rules.
        verbose (bool): Enables verbose logging for debugging purposes.

    Methods:
        _parse_html(html_content): Parses HTML content into a BeautifulSoup object.
        _get_base_elements(parsed_html, selector): Selects base elements using a CSS selector.
        _get_elements(element, selector): Selects child elements using a CSS selector.
        _get_element_text(element): Extracts text content from a BeautifulSoup element.
        _get_element_html(element): Extracts the raw HTML content of a BeautifulSoup element.
        _get_element_attribute(element, attribute): Retrieves an attribute value from a BeautifulSoup element.
    """

    def __init__(self, schema: Dict[str, Any], **kwargs):
        kwargs["input_format"] = "html"  # Force HTML input
        super().__init__(schema, **kwargs)

    def _parse_html(self, html_content: str):
        # return BeautifulSoup(html_content, "html.parser")
        return BeautifulSoup(html_content, "lxml")

    def _get_base_elements(self, parsed_html, selector: str):
        return parsed_html.select(selector)

    def _get_elements(self, element, selector: str):
        # Return all matching elements using select() instead of select_one()
        # This ensures that we get all elements that match the selector, not just the first one
        return element.select(selector)

    def _get_element_text(self, element) -> str:
        return element.get_text(strip=True)

    def _get_element_html(self, element) -> str:
        return str(element)

    def _get_element_attribute(self, element, attribute: str):
        return element.get(attribute)

    def _resolve_source(self, element, source: str):
        source = source.strip()
        if not source.startswith("+"):
            return None
        sel = source[1:].strip()  # e.g. "tr", "tr.subtext", ".classname"
        parts = sel.split(".")
        tag = parts[0].strip() or None
        classes = [p.strip() for p in parts[1:] if p.strip()]
        kwargs = {}
        if classes:
            kwargs["class_"] = lambda c, _cls=classes: c and all(
                cl in c for cl in _cls
            )
        return element.find_next_sibling(tag, **kwargs)


class JsonLxmlExtractionStrategy(JsonElementExtractionStrategy):
    def __init__(self, schema: Dict[str, Any], **kwargs):
        kwargs["input_format"] = "html"
        super().__init__(schema, **kwargs)
        self._selector_cache = {}
        self._xpath_cache = {}
        self._result_cache = {}
        
        # Control selector optimization strategy
        self.use_caching = kwargs.get("use_caching", True)
        self.optimize_common_patterns = kwargs.get("optimize_common_patterns", True)
        
        # Load lxml dependencies once
        from lxml import etree, html
        from lxml.cssselect import CSSSelector
        self.etree = etree
        self.html_parser = html
        self.CSSSelector = CSSSelector
    
    def _parse_html(self, html_content: str):
        """Parse HTML content with error recovery"""
        try:
            parser = self.etree.HTMLParser(recover=True, remove_blank_text=True)
            return self.etree.fromstring(html_content, parser)
        except Exception as e:
            if self.verbose:
                print(f"Error parsing HTML, falling back to alternative method: {e}")
            try:
                return self.html_parser.fromstring(html_content)
            except Exception as e2:
                if self.verbose:
                    print(f"Critical error parsing HTML: {e2}")
                # Create minimal document as fallback
                return self.etree.Element("html")
    
    def _optimize_selector(self, selector_str):
        """Optimize common selector patterns for better performance"""
        if not self.optimize_common_patterns:
            return selector_str
            
        # Handle td:nth-child(N) pattern which is very common in table scraping
        import re
        if re.search(r'td:nth-child\(\d+\)', selector_str):
            return selector_str  # Already handled specially in _apply_selector
            
        # Split complex selectors into parts for optimization
        parts = selector_str.split()
        if len(parts) <= 1:
            return selector_str
            
        # For very long selectors, consider using just the last specific part
        if len(parts) > 3 and any(p.startswith('.') or p.startswith('#') for p in parts):
            specific_parts = [p for p in parts if p.startswith('.') or p.startswith('#')]
            if specific_parts:
                return specific_parts[-1]  # Use most specific class/id selector
                
        return selector_str
    
    def _create_selector_function(self, selector_str):
        """Create a selector function that handles all edge cases"""
        original_selector = selector_str
        
        # Try to optimize the selector if appropriate
        if self.optimize_common_patterns:
            selector_str = self._optimize_selector(selector_str)
        
        try:
            # Attempt to compile the CSS selector
            compiled = self.CSSSelector(selector_str)
            xpath = compiled.path
            
            # Store XPath for later use
            self._xpath_cache[selector_str] = xpath
            
            # Create the wrapper function that implements the selection strategy
            def selector_func(element, context_sensitive=True):
                cache_key = None
                
                # Use result caching if enabled
                if self.use_caching:
                    # Create a cache key based on element and selector
                    element_id = element.get('id', '') or str(hash(element))
                    cache_key = f"{element_id}::{selector_str}"
                    
                    if cache_key in self._result_cache:
                        return self._result_cache[cache_key]
                
                results = []
                try:
                    # Strategy 1: Direct CSS selector application (fastest)
                    results = compiled(element)
                    
                    # If that fails and we need context sensitivity
                    if not results and context_sensitive:
                        # Strategy 2: Try XPath with context adjustment
                        context_xpath = self._make_context_sensitive_xpath(xpath, element)
                        if context_xpath:
                            results = element.xpath(context_xpath)
                        
                        # Strategy 3: Handle special case - nth-child
                        if not results and 'nth-child' in original_selector:
                            results = self._handle_nth_child_selector(element, original_selector)
                        
                        # Strategy 4: Direct descendant search for class/ID selectors
                        if not results:
                            results = self._fallback_class_id_search(element, original_selector)
                            
                        # Strategy 5: Last resort - tag name search for the final part
                        if not results:
                            parts = original_selector.split()
                            if parts:
                                last_part = parts[-1]
                                # Extract tag name from the selector
                                tag_match = re.match(r'^(\w+)', last_part)
                                if tag_match:
                                    tag_name = tag_match.group(1)
                                    results = element.xpath(f".//{tag_name}")
                    
                    # Cache results if caching is enabled
                    if self.use_caching and cache_key:
                        self._result_cache[cache_key] = results
                        
                except Exception as e:
                    if self.verbose:
                        print(f"Error applying selector '{selector_str}': {e}")
                
                return results
                
            return selector_func
            
        except Exception as e:
            if self.verbose:
                print(f"Error compiling selector '{selector_str}': {e}")
            
            # Fallback function for invalid selectors
            return lambda element, context_sensitive=True: []
    
    def _make_context_sensitive_xpath(self, xpath, element):
        """Convert absolute XPath to context-sensitive XPath"""
        try:
            # If starts with descendant-or-self, it's already context-sensitive
            if xpath.startswith('descendant-or-self::'):
                return xpath
                
            # Remove leading slash if present
            if xpath.startswith('/'):
                context_xpath = f".{xpath}"
            else:
                context_xpath = f".//{xpath}"
                
            # Validate the XPath by trying it
            try:
                element.xpath(context_xpath)
                return context_xpath
            except:
                # If that fails, try a simpler descendant search
                return f".//{xpath.split('/')[-1]}"
        except:
            return None
    
    def _handle_nth_child_selector(self, element, selector_str):
        """Special handling for nth-child selectors in tables"""
        import re
        results = []
        
        try:
            # Extract the column number from td:nth-child(N)
            match = re.search(r'td:nth-child\((\d+)\)', selector_str)
            if match:
                col_num = match.group(1)
                
                # Check if there's content after the nth-child part
                remaining_selector = selector_str.split(f"td:nth-child({col_num})", 1)[-1].strip()
                
                if remaining_selector:
                    # If there's a specific element we're looking for after the column
                    # Extract any tag names from the remaining selector
                    tag_match = re.search(r'(\w+)', remaining_selector)
                    tag_name = tag_match.group(1) if tag_match else '*'
                    results = element.xpath(f".//td[{col_num}]//{tag_name}")
                else:
                    # Just get the column cell
                    results = element.xpath(f".//td[{col_num}]")
        except Exception as e:
            if self.verbose:
                print(f"Error handling nth-child selector: {e}")
                
        return results
    
    def _fallback_class_id_search(self, element, selector_str):
        """Fallback to search by class or ID"""
        results = []
        
        try:
            # Extract class selectors (.classname)
            import re
            class_matches = re.findall(r'\.([a-zA-Z0-9_-]+)', selector_str)
            
            # Extract ID selectors (#idname)
            id_matches = re.findall(r'#([a-zA-Z0-9_-]+)', selector_str)
            
            # Try each class
            for class_name in class_matches:
                class_results = element.xpath(f".//*[contains(@class, '{class_name}')]")
                results.extend(class_results)
                
            # Try each ID (usually more specific)
            for id_name in id_matches:
                id_results = element.xpath(f".//*[@id='{id_name}']")
                results.extend(id_results)
        except Exception as e:
            if self.verbose:
                print(f"Error in fallback class/id search: {e}")
                
        return results
    
    def _get_selector(self, selector_str):
        """Get or create a selector function with caching"""
        if selector_str not in self._selector_cache:
            self._selector_cache[selector_str] = self._create_selector_function(selector_str)
        return self._selector_cache[selector_str]
    
    def _get_base_elements(self, parsed_html, selector: str):
        """Get all base elements using the selector"""
        selector_func = self._get_selector(selector)
        # For base elements, we don't need context sensitivity
        return selector_func(parsed_html, context_sensitive=False)
    
    def _get_elements(self, element, selector: str):
        """Get child elements using the selector with context sensitivity"""
        selector_func = self._get_selector(selector)
        return selector_func(element, context_sensitive=True)
    
    def _get_element_text(self, element) -> str:
        """Extract normalized text from element"""
        try:
            # Get all text nodes and normalize
            text = " ".join(t.strip() for t in element.xpath(".//text()") if t.strip())
            return text
        except Exception as e:
            if self.verbose:
                print(f"Error extracting text: {e}")
            # Fallback
            try:
                return element.text_content().strip()
            except:
                return ""
    
    def _get_element_html(self, element) -> str:
        """Get HTML string representation of element"""
        try:
            return self.etree.tostring(element, encoding='unicode', method='html')
        except Exception as e:
            if self.verbose:
                print(f"Error serializing HTML: {e}")
            return ""
    
    def _get_element_attribute(self, element, attribute: str):
        """Get attribute value safely"""
        try:
            return element.get(attribute)
        except Exception as e:
            if self.verbose:
                print(f"Error getting attribute '{attribute}': {e}")
            return None

    def _resolve_source(self, element, source: str):
        source = source.strip()
        if not source.startswith("+"):
            return None
        sel = source[1:].strip()
        parts = sel.split(".")
        tag = parts[0].strip() or "*"
        classes = [p.strip() for p in parts[1:] if p.strip()]
        xpath = f"./following-sibling::{tag}"
        for cls in classes:
            xpath += f"[contains(concat(' ',normalize-space(@class),' '),' {cls} ')]"
        xpath += "[1]"
        results = element.xpath(xpath)
        return results[0] if results else None

    def _clear_caches(self):
        """Clear caches to free memory"""
        if self.use_caching:
            self._result_cache.clear()


class JsonXPathExtractionStrategy(JsonElementExtractionStrategy):
    """
    Concrete implementation of `JsonElementExtractionStrategy` using XPath selectors.

    How it works:
    1. Parses HTML content into an lxml tree.
    2. Selects elements using XPath expressions.
    3. Converts CSS selectors to XPath when needed.

    Attributes:
        schema (Dict[str, Any]): The schema defining the extraction rules.
        verbose (bool): Enables verbose logging for debugging purposes.

    Methods:
        _parse_html(html_content): Parses HTML content into an lxml tree.
        _get_base_elements(parsed_html, selector): Selects base elements using an XPath selector.
        _css_to_xpath(css_selector): Converts a CSS selector to an XPath expression.
        _get_elements(element, selector): Selects child elements using an XPath selector.
        _get_element_text(element): Extracts text content from an lxml element.
        _get_element_html(element): Extracts the raw HTML content of an lxml element.
        _get_element_attribute(element, attribute): Retrieves an attribute value from an lxml element.
    """

    def __init__(self, schema: Dict[str, Any], **kwargs):
        kwargs["input_format"] = "html"  # Force HTML input
        super().__init__(schema, **kwargs)

    def _parse_html(self, html_content: str):
        return html.fromstring(html_content)

    def _get_base_elements(self, parsed_html, selector: str):
        return parsed_html.xpath(selector)

    def _css_to_xpath(self, css_selector: str) -> str:
        """Convert CSS selector to XPath if needed"""
        if "/" in css_selector:  # Already an XPath
            return css_selector
        return self._basic_css_to_xpath(css_selector)

    def _basic_css_to_xpath(self, css_selector: str) -> str:
        """Basic CSS to XPath conversion for common cases"""
        if " > " in css_selector:
            parts = css_selector.split(" > ")
            return "//" + "/".join(parts)
        if " " in css_selector:
            parts = css_selector.split(" ")
            return "//" + "//".join(parts)
        return "//" + css_selector

    def _get_elements(self, element, selector: str):
        xpath = self._css_to_xpath(selector)
        if not xpath.startswith("."):
            xpath = "." + xpath
        return element.xpath(xpath)

    def _get_element_text(self, element) -> str:
        return "".join(element.xpath(".//text()")).strip()

    def _get_element_html(self, element) -> str:
        return etree.tostring(element, encoding="unicode")

    def _get_element_attribute(self, element, attribute: str):
        return element.get(attribute)

    def _resolve_source(self, element, source: str):
        source = source.strip()
        if not source.startswith("+"):
            return None
        sel = source[1:].strip()
        parts = sel.split(".")
        tag = parts[0].strip() or "*"
        classes = [p.strip() for p in parts[1:] if p.strip()]
        xpath = f"./following-sibling::{tag}"
        for cls in classes:
            xpath += f"[contains(concat(' ',normalize-space(@class),' '),' {cls} ')]"
        xpath += "[1]"
        results = element.xpath(xpath)
        return results[0] if results else None


_CTRL = {c: rf"\x{ord(c):02x}" for c in map(chr, range(32)) if c not in "\t\n\r"}

_WB_FIX = re.compile(r"\x08")               # stray back-space   →   word-boundary
_NEEDS_ESCAPE = re.compile(r"(?<!\\)\\(?![\\u])")   # lone backslash

def _sanitize_schema(schema: Dict[str, str]) -> Dict[str, str]:
    """Fix common JSON-escape goofs coming from LLMs or manual edits."""
    safe = {}
    for label, pat in schema.items():
        # 1️⃣ replace accidental control chars (inc. the infamous back-space)
        pat = _WB_FIX.sub(r"\\b", pat).translate(_CTRL)

        # 2️⃣ double any single backslash that JSON kept single
        pat = _NEEDS_ESCAPE.sub(r"\\\\", pat)

        # 3️⃣ quick sanity compile
        try:
            re.compile(pat)
        except re.error as e:
            raise ValueError(f"Regex for '{label}' won’t compile after fix: {e}") from None

        safe[label] = pat
    return safe


class RegexExtractionStrategy(ExtractionStrategy):
    """
    A lean strategy that finds e-mails, phones, URLs, dates, money, etc.,
    using nothing but pre-compiled regular expressions.

    Extraction returns::

        {
            "url":   "<page-url>",
            "label": "<pattern-label>",
            "value": "<matched-string>",
            "span":  [start, end]
        }

    Only `generate_schema()` touches an LLM, extraction itself is pure Python.
    """

    # -------------------------------------------------------------- #
    # Built-in patterns exposed as IntFlag so callers can bit-OR them
    # -------------------------------------------------------------- #
    class _B(IntFlag):
        EMAIL           = auto()
        PHONE_INTL      = auto()
        PHONE_US        = auto()
        URL             = auto()
        IPV4            = auto()
        IPV6            = auto()
        UUID            = auto()
        CURRENCY        = auto()
        PERCENTAGE      = auto()
        NUMBER          = auto()
        DATE_ISO        = auto()
        DATE_US         = auto()
        TIME_24H        = auto()
        POSTAL_US       = auto()
        POSTAL_UK       = auto()
        HTML_COLOR_HEX  = auto()
        TWITTER_HANDLE  = auto()
        HASHTAG         = auto()
        MAC_ADDR        = auto()
        IBAN            = auto()
        CREDIT_CARD     = auto()
        NOTHING         = auto()
        ALL             = (
            EMAIL | PHONE_INTL | PHONE_US | URL | IPV4 | IPV6 | UUID
            | CURRENCY | PERCENTAGE | NUMBER | DATE_ISO | DATE_US | TIME_24H
            | POSTAL_US | POSTAL_UK | HTML_COLOR_HEX | TWITTER_HANDLE
            | HASHTAG | MAC_ADDR | IBAN | CREDIT_CARD
        )

    # user-friendly aliases  (RegexExtractionStrategy.Email, .IPv4, …)
    Email          = _B.EMAIL
    PhoneIntl      = _B.PHONE_INTL
    PhoneUS        = _B.PHONE_US
    Url            = _B.URL
    IPv4           = _B.IPV4
    IPv6           = _B.IPV6
    Uuid           = _B.UUID
    Currency       = _B.CURRENCY
    Percentage     = _B.PERCENTAGE
    Number         = _B.NUMBER
    DateIso        = _B.DATE_ISO
    DateUS         = _B.DATE_US
    Time24h        = _B.TIME_24H
    PostalUS       = _B.POSTAL_US
    PostalUK       = _B.POSTAL_UK
    HexColor       = _B.HTML_COLOR_HEX
    TwitterHandle  = _B.TWITTER_HANDLE
    Hashtag        = _B.HASHTAG
    MacAddr        = _B.MAC_ADDR
    Iban           = _B.IBAN
    CreditCard     = _B.CREDIT_CARD
    All            = _B.ALL
    Nothing        = _B(0)  # no patterns

    # ------------------------------------------------------------------ #
    # Built-in pattern catalog
    # ------------------------------------------------------------------ #
    DEFAULT_PATTERNS: Dict[str, str] = {
        # Communication
        "email":           r"[\w.+-]+@[\w-]+\.[\w.-]+",
        "phone_intl":      r"\+?\d[\d .()-]{7,}\d",
        "phone_us":        r"\(?\d{3}\)?[ -. ]?\d{3}[ -. ]?\d{4}",
        # Web
        "url":             r"https?://[^\s\"'<>]+",
        "ipv4":            r"(?:\d{1,3}\.){3}\d{1,3}",
        "ipv6":            r"[A-F0-9]{1,4}(?::[A-F0-9]{1,4}){7}",
        # IDs
        "uuid":            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        # Money / numbers
        "currency":        r"(?:USD|EUR|RM|\$|€|£)\s?\d+(?:[.,]\d{2})?",
        "percentage":      r"\d+(?:\.\d+)?%",
        "number":          r"\b\d{1,3}(?:[,.\s]\d{3})*(?:\.\d+)?\b",
        # Dates / Times
        "date_iso":        r"\d{4}-\d{2}-\d{2}",
        "date_us":         r"\d{1,2}/\d{1,2}/\d{2,4}",
        "time_24h":        r"\b(?:[01]?\d|2[0-3]):[0-5]\d(?:[:.][0-5]\d)?\b",
        # Misc
        "postal_us":       r"\b\d{5}(?:-\d{4})?\b",
        "postal_uk":       r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b",
        "html_color_hex":  r"#[0-9A-Fa-f]{6}\b",
        "twitter_handle":  r"@[\w]{1,15}",
        "hashtag":         r"#[\w-]+",
        "mac_addr":        r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}",
        "iban":            r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}",
        "credit_card":     r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12})\b",
    }

    _FLAGS = re.IGNORECASE | re.MULTILINE
    _UNWANTED_PROPS = {
        "provider": "Use llm_config instead",
        "api_token": "Use llm_config instead",
    }

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        pattern: "_B" = _B.NOTHING,
        *,
        custom: Optional[Union[Dict[str, str], List[Tuple[str, str]]]] = None,
        input_format: str = "fit_html",
        **kwargs,
    ) -> None:
        """
        Args:
            patterns: Custom patterns overriding or extending defaults.
                      Dict[label, regex] or list[tuple(label, regex)].
            input_format: "html", "markdown" or "text".
            **kwargs: Forwarded to ExtractionStrategy.
        """
        super().__init__(input_format=input_format, **kwargs)

        # 1️⃣  take only the requested built-ins
        merged: Dict[str, str] = {
            key: rx
            for key, rx in self.DEFAULT_PATTERNS.items()
            if getattr(self._B, key.upper()).value & pattern
        }

        # 2️⃣  apply user overrides / additions
        if custom:
            if isinstance(custom, dict):
                merged.update(custom)
            else:  # iterable of (label, regex)
                merged.update({lbl: rx for lbl, rx in custom})

        self._compiled: Dict[str, Pattern] = {
            lbl: re.compile(rx, self._FLAGS) for lbl, rx in merged.items()
        }

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def extract(self, url: str, content: str, *q, **kw) -> List[Dict[str, Any]]:
        # text = self._plain_text(html)
        out: List[Dict[str, Any]] = []

        for label, cre in self._compiled.items():
            for m in cre.finditer(content):
                out.append(
                    {
                        "url": url,
                        "label": label,
                        "value": m.group(0),
                        "span": [m.start(), m.end()],
                    }
                )
        return out

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _plain_text(self, content: str) -> str:
        if self.input_format == "text":
            return content
        return BeautifulSoup(content, "lxml").get_text(" ", strip=True)

    # ------------------------------------------------------------------ #
    # LLM-assisted pattern generator
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # LLM-assisted one-off pattern builder
    # ------------------------------------------------------------------ #
