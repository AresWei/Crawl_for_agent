"use strict";
// The real toolbar invocation captures the original source tab before opening a
// durable extension page. No Cookie data is read here; no background observer.
(async () => {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const url = new URL(tab.url);
  const supported = new Set(["www.xiaohongshu.com", "www.douyin.com", "www.kuaishou.com", "www.bilibili.com", "weibo.com", "tieba.baidu.com", "www.zhihu.com", "www.reddit.com"]);
  if (!Number.isInteger(tab.id) || tab.incognito || url.protocol !== "https:" || !supported.has(url.hostname)) {
    throw new Error("请在所选平台主站的普通标签页点击扩展图标。");
  }
  const stores = await chrome.cookies.getAllCookieStores();
  const store = stores.find(item => item.tabIds.includes(tab.id));
  if (!store) throw new Error("未找到来源标签页的 Cookie 容器。");
  await chrome.storage.session.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
  await chrome.storage.session.set({["source:" + tab.id]: {
    tabId: tab.id, windowId: tab.windowId, storeId: store.id, site: url.hostname
  }});
  await chrome.tabs.create({url: chrome.runtime.getURL("popup.html?source_tab=" + tab.id), windowId: tab.windowId});
  window.close();
})().catch(error => { document.getElementById("message").textContent = error.message || "无法打开导入页，请回到所选主站重试。"; });
