"""Bounded local/raw/PDF ingestion; PDF processing is lazily imported."""
import json
import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from urllib.parse import unquote, urlsplit


def local_path(url):
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("Only explicit local file URLs are supported")
    value = unquote(parsed.path)
    if os.name == "nt" and len(value) > 2 and value[0] == "/" and value[2] == ":":
        value = value[1:]
    return Path(value).resolve(strict=True)


def read_local(url, maximum):
    path = local_path(url)
    if not path.is_file():
        raise ValueError("Input is not a regular file")
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError("local_input_byte_budget")
    return content, path


def process_pdf(path, params, image_dir):
    from .processors.pdf.processor import NaivePDFProcessorStrategy
    processor = NaivePDFProcessorStrategy(extract_images=bool(params.get("pdf_extract_images", False)),
        save_images_locally=bool(params.get("pdf_extract_images", False)), image_save_dir=image_dir,
        max_pages=max(1, min(1000, int(params.get("pdf_max_pages", 100)))))
    result = processor.process(path)
    data = json.loads(json.dumps(asdict(result), default=str))
    # Publish only processor-reported files inside this job's assigned directory.
    # This feeds the existing authenticated artifact download route; no separate
    # output owner or directory scan is introduced.
    data["image_artifacts"] = {}
    root = Path(image_dir).resolve()
    for page in data["pages"]:
        for index, image in enumerate(page["images"], 1):
            if not image.get("path"):
                continue
            output = Path(image["path"]).resolve()
            if not output.is_relative_to(root) or not output.is_file():
                raise ValueError("PDF image artifact is outside the assigned output directory")
            with output.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            image.update(path=str(output), sha256=digest, bytes=output.stat().st_size,
                         page_number=page["page_number"], source_field="PDF page image XObject")
            data["image_artifacts"][f"pdf_image_{page['page_number']}_{index}"] = str(output)
    return "\n".join(p.html for p in result.pages), data
