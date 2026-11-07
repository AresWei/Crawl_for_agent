"""HTTP text decoding adapted from Crawl4AI's HTTP response boundary.

Source: async_crawler_strategy.py, pinned 838869bdda93f25e6d50b3f50ae9577d9b19a586.
Apache-2.0; LICENSE.crawl4ai and NOTICE apply. The caller owns bytes/network limits.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass, asdict
from email.message import Message
from html.parser import HTMLParser
import re
from typing import Mapping


class BinaryContentError(ValueError):
    """The response belongs to the download/document path, not HTML decoding."""


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    source: str
    content_type: str
    source_bytes: int
    replacement_characters: int
    warnings: tuple[str, ...]
    detection: dict | None = None

    def evidence(self) -> dict:
        """Metadata only: source text is returned separately, never duplicated here."""
        values = asdict(self)
        values.pop('text')
        values['warnings'] = list(self.warnings)
        return values


_TEXT_TYPES = frozenset({
    'application/json', 'application/xml', 'application/xhtml+xml',
    'application/rss+xml', 'application/atom+xml', 'application/ld+json',
    'application/x-ndjson', 'application/javascript', 'application/x-javascript',
})
_BOMS = ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
         (codecs.BOM_UTF8, 'utf-8-sig'), (codecs.BOM_UTF16_LE, 'utf-16'),
         (codecs.BOM_UTF16_BE, 'utf-16'))
_SNIFF_BYTES = 8192
_DETECTION_BYTES = 65536


def is_text_content_type(value: str | None) -> bool:
    mime = (value or '').split(';', 1)[0].strip().lower()
    return (not mime or mime.startswith('text/') or mime in _TEXT_TYPES
            or mime.endswith('+json') or mime.endswith('+xml'))


def _codec(label: str) -> str:
    if not isinstance(label, str) or not label.strip() or len(label) > 100:
        raise ValueError('Invalid text encoding')
    try:
        canonical = codecs.lookup(label.strip()).name
        # Empty bytes take a Python fast path which does not validate codec kind.
        b'x'.decode(canonical, errors='ignore')  # Reject non-text codecs such as base64_codec.
        return canonical
    except (LookupError, ValueError, TypeError) as error:
        raise ValueError('Unknown or unsupported text encoding') from error


def _header_charset(content_type: str) -> str | None:
    message = Message()
    message['content-type'] = content_type
    return message.get_content_charset()


class _MetaCharset(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.charset = None

    def handle_starttag(self, tag, attrs):
        if tag != 'meta' or self.charset:
            return
        values = dict(attrs)
        if values.get('charset'):
            self.charset = values['charset']
        elif (values.get('http-equiv') or '').lower() == 'content-type':
            self.charset = _header_charset(values.get('content') or '')


def _document_charset(data: bytes, content_type: str) -> tuple[str | None, str | None]:
    sample = data[:_SNIFF_BYTES].decode('ascii', errors='ignore')
    declaration = re.match(r'''\s*<\?xml\s+[^>]*?encoding\s*=\s*['"]([^'"]+)['"]''', sample, re.I)
    if declaration:
        return declaration.group(1), 'xml'
    mime = content_type.split(';', 1)[0].strip().lower()
    if mime not in ('', 'text/html', 'application/xhtml+xml'):
        return None, None
    parser = _MetaCharset()
    parser.feed(sample)
    return parser.charset, 'html_meta' if parser.charset else None


def _binary_signature(data: bytes) -> bool:
    signatures = (b'%PDF-', b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a',
                  b'PK\x03\x04', b'PK\x05\x06', b'\x1f\x8b', b'fLaC', b'ID3', b'OggS')
    return (data.startswith(signatures)
            or (data[:4] == b'RIFF' and data[8:12] in (b'WEBP', b'WAVE', b'AVI '))
            or data[4:8] == b'ftyp')


def decode_http_text(data: bytes, headers: Mapping[str, str] | None = None, *,
                     encoding: str | None = None, detect: bool = True) -> DecodedText:
    """Decode bounded response bytes without confusing HTTPX's default with a declaration.

    Precedence: explicit override, BOM, valid HTTP charset, HTML/XML declaration,
    strict UTF-8, lazy chardet, UTF-8 replacement fallback. Binary MIME/signatures
    raise BinaryContentError even with an encoding override. Detection confidence
    is chardet's actual heuristic output, not a probability of text correctness.
    """
    if not isinstance(data, bytes):
        raise TypeError('data must be bytes')
    values = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    content_type = values.get('content-type', '')
    if not is_text_content_type(content_type) or _binary_signature(data):
        raise BinaryContentError('Response is binary; use the document or media-download path')
    warnings = []
    declarations = []
    bom = next((name for marker, name in _BOMS if data.startswith(marker)), None)
    if bom:
        declarations.append(('bom', bom))
    header = _header_charset(content_type)
    document, document_source = _document_charset(data, content_type)
    for source, label in (('http_charset', header), (document_source, document)):
        if label:
            try:
                declarations.append((source, _codec(label)))
            except ValueError:
                warnings.append('invalid_' + source)
    if encoding is not None:
        declarations.insert(0, ('explicit', _codec(encoding)))
    selected = declarations[0] if declarations else None
    # NUL-bearing input without an actual Unicode declaration is binary evidence,
    # not a reason to make an arbitrary UTF-16 guess.
    if b'\x00' in data[:_SNIFF_BYTES] and not (selected and selected[1].startswith(('utf-16', 'utf-32'))):
        raise BinaryContentError('Undeclared NUL-bearing content cannot be treated as HTML')
    detection = None
    if selected is None:
        try:
            data.decode('utf-8', errors='strict')
            selected = ('utf8_valid', 'utf-8')
        except UnicodeDecodeError:
            if detect:
                try:
                    import chardet
                except ImportError:
                    warnings.append('detector_unavailable')
                else:
                    sample = data[:_DETECTION_BYTES]
                    detected = chardet.detect(sample)
                    detection = {'detector': 'chardet', 'version': chardet.__version__,
                                 'sample_bytes': len(sample), 'source_bytes': len(data),
                                 'encoding': detected.get('encoding'),
                                 'confidence': detected.get('confidence'),
                                 'language': detected.get('language')}
                    candidate = detected.get('encoding')
                    if candidate:
                        try:
                            selected = ('detected', _codec(candidate))
                        except ValueError:
                            warnings.append('invalid_detected_charset')
                    if selected and (detected.get('confidence') or 0) < .5:
                        warnings.append('low_detector_confidence')
            if selected is None:
                selected = ('utf8_fallback', 'utf-8')
                warnings.append('undetermined_charset')
    source, chosen = selected
    family = lambda value: value.replace('-sig', '').replace('-le', '').replace('-be', '')
    if any(family(value) != family(chosen) for _, value in declarations):
        warnings.append('conflicting_charset_declarations')
    try:
        text = data.decode(chosen, errors='strict')
        replacements = 0
    except UnicodeDecodeError:
        text = data.decode(chosen, errors='replace')
        replacements = text.count('\ufffd')
        warnings.append('decoding_replacements')
    return DecodedText(text, chosen, source, content_type, len(data), replacements,
                       tuple(warnings), detection)
