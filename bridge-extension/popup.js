"use strict";
const BASE = "http://127.0.0.1:8946/bridge/v1/";
const SUPPORTED = new Set(["www.xiaohongshu.com", "www.douyin.com", "www.kuaishou.com", "www.bilibili.com", "weibo.com", "tieba.baidu.com", "www.zhihu.com", "www.reddit.com"]);
const $ = id => document.getElementById(id);
let pairing = null;
let currentTab = null;
const sourceTabId = Number(new URL(location.href).searchParams.get("source_tab"));
const pairingKey = "pairing:" + sourceTabId;
let source = null;
function permissionOrigins(site) {
  // Chrome checks each cookie's own domain and its Secure-derived scheme, not
  // only the HTTPS query URL. These are exact XHS hosts, never wildcard domains.
  return site === "www.xiaohongshu.com"
    ? ["https://www.xiaohongshu.com/*", "http://www.xiaohongshu.com/*", "https://xiaohongshu.com/*", "http://xiaohongshu.com/*"]
    : ["https://" + site + "/*"];
}

function say(message) { $("message").textContent = message; }
function matches(tab, site) {
  try { const url = new URL(tab.url); return !!source && tab.id === source.tabId && tab.windowId === source.windowId && url.protocol === "https:" && url.hostname === site && site === source.site && !tab.incognito; }
  catch { return false; }
}
async function request(operation, value, token) {
  let response;
  try {
    response = await fetch(BASE + operation, {method: "POST", credentials: "omit", redirect: "error",
      headers: {"Content-Type": "application/json", ...(token ? {"X-Bridge-Token": token} : {})},
      body: JSON.stringify(value), signal: AbortSignal.timeout(10000)});
  } catch { throw new Error("无法连接本机工作台，请确认 127.0.0.1:8946 正在运行。"); }
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "请求未完成，请重新配对。");
  return result;
}
function render() {
  $("pair-panel").hidden = !!pairing;
  $("import-panel").hidden = !pairing;
  if (pairing) {
    $("site").textContent = pairing.site;
    $("account").textContent = pairing.platform + " / " + pairing.account;
    $("import").disabled = !$("confirm").checked || !matches(currentTab, pairing.site);
    if (!matches(currentTab, pairing.site)) say("来源标签页已改变或关闭，请从所选主站重新打开扩展。");
  }
}
async function forget() { pairing = null; await chrome.storage.session.remove(pairingKey); $("confirm").checked = false; render(); }
async function sourceStore(site) {
  const tab = await chrome.tabs.get(sourceTabId);
  if (!matches(tab, site)) throw new Error("来源标签页已改变或关闭，未导入 Cookie。");
  const stores = await chrome.cookies.getAllCookieStores();
  const store = stores.find(item => item.id === source.storeId && item.tabIds.includes(sourceTabId));
  if (!store) throw new Error("来源标签页的 Cookie 容器已改变，未导入。");
  return store;
}

$("pair").addEventListener("click", async () => {
  $("pair").disabled = true;
  try {
    const code = $("code").value.trim();
    if (!/^[a-fA-F0-9]{20}$/.test(code)) throw new Error("请输入工作台生成的 20 位配对码。");
    const result = await request("pair", {code});
    if (!SUPPORTED.has(result.site) || result.site !== source?.site || typeof result.token !== "string") throw new Error("配对站点与原来选定的来源不一致，请撤销此配对并重新选择。");
    pairing = result;
    await chrome.storage.session.set({[pairingKey]: pairing});
    $("code").value = "";
    say("请核对所选账号，再授权导入。"); render();
  } catch (error) { say(error.message); }
  finally { $("pair").disabled = false; }
});
$("confirm").addEventListener("change", render);
$("import").addEventListener("click", async () => {
  if (!pairing || !$("confirm").checked || !matches(currentTab, pairing.site)) return;
  const selected = pairing;
  const origins = permissionOrigins(selected.site);
  $("import").disabled = true;
  try {
    // Permission request is the first asynchronous operation in the click gesture.
    if (!await chrome.permissions.request({origins})) throw new Error("未授予站点访问权限，未读取 Cookie。");
    const store = await sourceStore(selected.site);
    // No wildcard query; URL and tab-associated cookie store are both mandatory.
    const raw = await chrome.cookies.getAll({url: "https://" + selected.site + "/", storeId: store.id});
    const skipped = raw.filter(cookie => cookie.partitionKey).length;
    const cookies = raw.filter(cookie => !cookie.partitionKey).map(cookie => ({
      name: cookie.name, value: cookie.value,
      domain: cookie.hostOnly ? cookie.domain.replace(/^\./, "") : "." + cookie.domain.replace(/^\./, ""),
      path: cookie.path, expires: cookie.session ? -1 : cookie.expirationDate,
      httpOnly: cookie.httpOnly, secure: cookie.secure,
      sameSite: ({strict: "Strict", lax: "Lax", no_restriction: "None", unspecified: "Lax"})[cookie.sameSite]
    }));
    if (!cookies.length) throw new Error("在所选来源容器与已授权域范围内没有读到可导入 Cookie；这不能单独证明未登录。");
    await sourceStore(selected.site);
    const result = await request("import", {site: selected.site, cookies}, selected.token);
    await forget();
    say("已保存 " + result.cookie_count + " 条 Cookie，需实际采集验证登录。" + (skipped ? "已跳过分区 Cookie。" : ""));
  } catch (error) { say(error.message); }
  finally { await chrome.permissions.remove({origins}); render(); }
});
$("revoke").addEventListener("click", async () => {
  try { if (pairing) await request("revoke", {}, pairing.token); await forget(); say("配对已撤销。已导入的账号状态需在工作台单独撤销。"); }
  catch (error) { say(error.message); }
});
(async () => {
  await chrome.storage.session.setAccessLevel({accessLevel: "TRUSTED_CONTEXTS"});
  if (!Number.isInteger(sourceTabId) || sourceTabId <= 0) throw new Error("请从平台主站的扩展图标打开此导入页。");
  source = (await chrome.storage.session.get("source:" + sourceTabId))["source:" + sourceTabId];
  if (!source || source.tabId !== sourceTabId) throw new Error("来源选择已失效，请从主站重新打开扩展。");
  currentTab = await chrome.tabs.get(sourceTabId);
  let host = "不支持的页面";
  try { host = new URL(currentTab.url).hostname; } catch {}
  $("current").textContent = "来源页面 · " + host;
  pairing = (await chrome.storage.session.get(pairingKey))[pairingKey] || null;
  if (pairing && (!SUPPORTED.has(pairing.site) || pairing.expires_at * 1000 <= Date.now())) await forget();
  render();
})().catch(error => { $("pair").disabled = true; say(error.message || "扩展初始化失败，请从主站重新打开。"); });
