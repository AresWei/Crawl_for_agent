// Derived from Crawl4AI commit 838869bdda93f25e6d50b3f50ae9577d9b19a586.
// Copyright Crawl4AI contributors. See ../LICENSE.crawl4ai and source-manifest.json.
async (config) => {
                const container = document.querySelector(config.container_selector);
                if (!container) {
                    throw new Error(`Container not found: ${config.container_selector}`);
                }
                
                // List to store HTML chunks when content is replaced
                const htmlChunks = [];
                let previousHTML = container.innerHTML;
                let scrollCount = 0;
                
                // Determine scroll amount
                let scrollAmount;
                if (typeof config.scroll_by === 'number') {
                    scrollAmount = config.scroll_by;
                } else if (config.scroll_by === 'page_height') {
                    scrollAmount = window.innerHeight;
                } else { // container_height
                    scrollAmount = container.offsetHeight;
                }
                
                // Perform scrolling
                while (scrollCount < config.scroll_count) {
                    // Scroll the container
                    container.scrollTop += scrollAmount;
                    
                    // Wait for content to potentially load
                    await new Promise(resolve => setTimeout(resolve, config.wait_after_scroll * 1000));
                    
                    // Get current HTML
                    const currentHTML = container.innerHTML;
                    
                    // Determine what changed
                    if (currentHTML === previousHTML) {
                        // Case 0: No change - continue scrolling
                        console.log(`Scroll ${scrollCount + 1}: No change in content`);
                    } else if (currentHTML.startsWith(previousHTML)) {
                        // Case 1: New items appended - content already in page
                        console.log(`Scroll ${scrollCount + 1}: New items appended`);
                    } else {
                        // Case 2: Items replaced - capture the previous HTML
                        console.log(`Scroll ${scrollCount + 1}: Content replaced, capturing chunk`);
                        htmlChunks.push(previousHTML);
                    }
                    
                    // Update previous HTML for next iteration
                    previousHTML = currentHTML;
                    scrollCount++;
                    
                    // Check if we've reached the end
                    if (container.scrollTop + container.clientHeight >= container.scrollHeight - 10) {
                        console.log(`Reached end of scrollable content at scroll ${scrollCount}`);
                        // Capture final chunk if content was replaced
                        if (htmlChunks.length > 0) {
                            htmlChunks.push(currentHTML);
                        }
                        break;
                    }
                }
                
                // If we have chunks (case 2 occurred), merge them
                if (htmlChunks.length > 0) {
                    console.log(`Merging ${htmlChunks.length} HTML chunks`);
                    
                    // Parse all chunks to extract unique elements
                    const tempDiv = document.createElement('div');
                    const seenTexts = new Set();
                    const uniqueElements = [];
                    
                    // Process each chunk
                    for (const chunk of htmlChunks) {
                        tempDiv.innerHTML = chunk;
                        const elements = tempDiv.children;
                        
                        for (let i = 0; i < elements.length; i++) {
                            const element = elements[i];
                            // Normalize text for deduplication
                            const normalizedText = element.innerText
                                .toLowerCase()
                                .replace(/[\s\W]/g, ''); // Remove spaces and symbols
                            
                            if (!seenTexts.has(normalizedText)) {
                                seenTexts.add(normalizedText);
                                uniqueElements.push(element.outerHTML);
                            }
                        }
                    }
                    
                    // Replace container content with merged unique elements
                    container.innerHTML = uniqueElements.join('\n');
                    console.log(`Merged ${uniqueElements.length} unique elements from ${htmlChunks.length} chunks`);
                    
                    return {
                        success: true,
                        chunksCount: htmlChunks.length,
                        uniqueCount: uniqueElements.length,
                        replaced: true
                    };
                } else {
                    console.log('No content replacement detected, all content remains in page');
                    return {
                        success: true,
                        chunksCount: 0,
                        uniqueCount: 0,
                        replaced: false
                    };
                }
            }
