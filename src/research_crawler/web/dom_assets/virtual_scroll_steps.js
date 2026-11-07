// Derived from Crawl4AI _handle_virtual_scroll, commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
// Copyright Crawl4AI contributors. See ../LICENSE.crawl4ai and source-manifest.json.
// Local changes: host-controlled steps; bounded snapshots; final chunk retained;
// stable IDs/Unicode-safe deduplication; inert merged DOM with observation markers.
(config) => {
    const container = document.querySelector(config.container_selector);
    if (!container) return {error: 'container_not_found'};
    const encoder = new TextEncoder();
    if (config.action === 'scroll') {
        let amount;
        if (typeof config.scroll_by === 'number') amount = config.scroll_by;
        else if (config.scroll_by === 'page_height') amount = window.innerHeight;
        else amount = container.offsetHeight;
        container.scrollTop += amount;
        return {scroll_top: container.scrollTop};
    }
    if (config.action === 'merge') {
        const tempDiv = document.createElement('div');
        const unique = new Map();
        let bytes = 0;
        let stop = null;
        for (const chunk of config.chunks) {
            tempDiv.innerHTML = chunk;
            const elements = config.item_selector ? tempDiv.querySelectorAll(config.item_selector) : tempDiv.children;
            for (const element of elements) {
                const explicit = config.item_key_attribute && element.getAttribute(config.item_key_attribute);
                const stable = explicit || element.getAttribute('data-id') || element.id;
                const normalized = (element.textContent || '').normalize('NFKC').toLowerCase().replace(/\s+/gu, '');
                const key = stable ? 'id:' + stable : 'text:' + (normalized || element.outerHTML);
                if (unique.has(key)) continue;
                const html = element.outerHTML;
                const size = encoder.encode(html).length;
                if (bytes + size > config.max_bytes) {stop = 'dom_byte_budget'; break;}
                if (unique.size >= config.max_items) {stop = 'virtual_item_budget'; break;}
                unique.set(key, html);
                bytes += size;
            }
            if (stop) break;
        }
        // Preserve the original live container when the derived aggregate would truncate.
        if (stop) return {error: stop, unique_count: unique.size, bytes};
        // A live virtualizer can immediately recycle the merged children on a
        // scroll event. Freeze this derived view in a cloned container so the
        // observed aggregate survives serialization without running listeners.
        const aggregate = container.cloneNode(false);
        aggregate.innerHTML = [...unique.values()].join('\n');
        aggregate.setAttribute('data-research-virtual-aggregate', 'true');
        container.replaceWith(aggregate);
        return {unique_count: unique.size, bytes, chunks_count: config.chunks.length};
    }
    const html = container.innerHTML;
    const bytes = encoder.encode(html).length;
    if (bytes > config.max_bytes) return {error: 'dom_byte_budget', bytes};
    return {html, bytes, scroll_top: container.scrollTop,
            reached_end: container.scrollTop + container.clientHeight >= container.scrollHeight - 2};
}
