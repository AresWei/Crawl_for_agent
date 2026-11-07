// Derived from Crawl4AI process_iframes, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
// Copyright Crawl4AI contributors. See ../LICENSE.crawl4ai and source-manifest.json.
// Local changes: arguments instead of template interpolation; preserve source/id;
// resolve relative media/link URLs against the captured frame's source.
(iframe, capture) => {
    const div = document.createElement('div');
    const parser = new DOMParser();
    const doc = parser.parseFromString(capture.html, 'text/html');
    for (const element of doc.querySelectorAll('[href],[src],[poster]')) {
        for (const attribute of ['href', 'src', 'poster']) {
            const value = element.getAttribute(attribute);
            if (!value || value.startsWith('#')) continue;
            try { element.setAttribute(attribute, new URL(value, capture.base_url).href); } catch (_) {}
        }
    }
    while (doc.body.firstChild) div.appendChild(doc.body.firstChild);
    div.className = 'extracted-iframe-content-' + capture.index;
    div.setAttribute('data-research-iframe-source', capture.source_url);
    if (iframe.id) div.setAttribute('data-research-original-iframe-id', iframe.id);
    iframe.replaceWith(div);
    return true;
}
