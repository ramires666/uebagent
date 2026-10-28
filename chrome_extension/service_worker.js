import { PSL_RULES } from "./public_suffix_rules.js";

const DEFAULT_API_ORIGIN = "http://localhost:8080";
const RECONNECT_ALARM = "local-mcp-reconnect";
const MAX_MESSAGE_BYTES = 1024 * 1024;
const MAX_TASK_COOKIE_COUNT = 500;
const MAX_TASK_COOKIE_BYTES = MAX_MESSAGE_BYTES - 8192;
const CATALOG_KEY = "current_cookie_catalog";
const STATE_KEY = "extension_state";
const HEARTBEAT_MS = 20_000;

let socket = null;
let reconnecting = false;
let heartbeatTimer = null;

function validApiOrigin(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" &&
      ["localhost", "127.0.0.1"].includes(url.hostname) &&
      !url.username && !url.password && !url.search && !url.hash &&
      (url.pathname === "/" || url.pathname === "") &&
      Number(url.port || 80) > 0 && Number(url.port || 80) <= 65535;
  } catch {
    return false;
  }
}

async function getState() {
  const saved = await chrome.storage.local.get(["api_origin", "source_id", "token", "label", "pending_origins"]);
  return {
    api_origin: validApiOrigin(saved.api_origin) ? new URL(saved.api_origin).origin : DEFAULT_API_ORIGIN,
    source_id: saved.source_id || null,
    token: saved.token || null,
    label: saved.label || "My Chrome",
    pending_origins: Array.isArray(saved.pending_origins) ? saved.pending_origins : []
  };
}

async function setStatus(status, message = "") {
  const previous = await chrome.storage.local.get(STATE_KEY);
  await chrome.storage.local.set({ [STATE_KEY]: {
    ...(previous[STATE_KEY] || {}),
    status,
    status_message: String(message).slice(0, 240),
    updated_at: Date.now()
  } });
}

function safeOriginFromTab(tabUrl) {
  try {
    const url = new URL(tabUrl);
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password) return null;
    return url.origin;
  } catch {
    return null;
  }
}

function siteId(origin, storeId) {
  const bytes = new TextEncoder().encode(JSON.stringify([origin, storeId]));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function siteFromId(id) {
  try {
    if (typeof id !== "string" || id.length > 2048) return null;
    const encoded = id.replaceAll("-", "+").replaceAll("_", "/");
    const binary = atob(encoded + "=".repeat((4 - encoded.length % 4) % 4));
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const decoded = JSON.parse(new TextDecoder().decode(bytes));
    if (!Array.isArray(decoded) || decoded.length !== 2 || typeof decoded[0] !== "string" || typeof decoded[1] !== "string") return null;
    return { origin: decoded[0], store_id: decoded[1] };
  } catch {
    return null;
  }
}

async function enumerateOpenSites(serviceOrigin) {
  const [tabs, stores] = await Promise.all([
    chrome.tabs.query({ windowType: "normal" }),
    chrome.cookies.getAllCookieStores()
  ]);
  const storeByTab = new Map();
  for (const store of stores) {
    for (const tabId of store.tabIds || []) storeByTab.set(tabId, store.id);
  }
  const grouped = new Map();
  for (const tab of tabs) {
    if (tab.incognito || !Number.isInteger(tab.id)) continue;
    const origin = safeOriginFromTab(tab.url);
    if (!origin || origin === serviceOrigin) continue;
    const storeId = storeByTab.get(tab.id);
    if (typeof storeId !== "string") continue;
    const key = siteId(origin, storeId);
    const item = grouped.get(key) || { site_id: key, origin, store_id: storeId, tab_count: 0 };
    item.tab_count += 1;
    grouped.set(key, item);
  }
  return [...grouped.values()].sort((a, b) => a.origin.localeCompare(b.origin) || a.store_id.localeCompare(b.store_id));
}

function cookieIdentity(cookie) {
  const partition = cookie.partitionKey ? JSON.stringify(cookie.partitionKey) : "";
  return JSON.stringify([cookie.storeId || "", cookie.name, cookie.domain, cookie.path, Boolean(cookie.hostOnly), partition]);
}

function metadataFor(cookie, siteIds) {
  let unsupported = null;
  if (cookie.partitionKey) unsupported = "Partitioned cookies are not supported for transfer.";
  const normalizedDomain = String(cookie.domain || "").replace(/^\./u, "").toLowerCase();
  if (!unsupported && !cookie.hostOnly && isPublicSuffix(normalizedDomain)) {
    unsupported = "Cookie domain is a public suffix.";
  }
  return {
    source_key: cookieIdentity(cookie),
    site_ids: [...siteIds],
    name: cookie.name,
    domain: cookie.domain,
    path: cookie.path,
    store_id: cookie.storeId,
    host_only: Boolean(cookie.hostOnly),
    secure: Boolean(cookie.secure),
    http_only: Boolean(cookie.httpOnly),
    same_site: cookie.sameSite || "unspecified",
    expires_at: Number.isFinite(cookie.expirationDate) ? cookie.expirationDate : null,
    partition_key: cookie.partitionKey || null,
    unsupported_reason: unsupported
  };
}

function cookieAppliesToHost(cookie, hostname) {
  const domain = String(cookie.domain || "").replace(/^\./u, "").toLowerCase();
  const host = hostname.toLowerCase();
  if (!domain || !host) return false;
  if (cookie.hostOnly) return domain === host;
  return host === domain || host.endsWith(`.${domain}`);
}

async function readCookies(domain, storeId) {
  try {
    return await chrome.cookies.getAll({ domain, storeId });
  } catch {
    throw Object.assign(new Error("Chrome could not read the selected site's cookies."), {
      code: "CHROME_COOKIE_READ_FAILED"
    });
  }
}

// PSL rules include both ICANN and private sections. URL normalizes IDNs to ASCII,
// just as URL.hostname and Chrome's cookie domains do.
function asciiDomain(value) {
  try { return new URL(`https://${value}/`).hostname.toLowerCase().replace(/\.$/u, ""); }
  catch { return ""; }
}
const exactRules = new Set();
const wildcardRules = new Set();
const exceptionRules = new Set();
for (const rawRule of PSL_RULES) {
  const exception = rawRule.startsWith("!");
  const wildcard = rawRule.startsWith("*.");
  const domain = asciiDomain(rawRule.slice(exception ? 1 : wildcard ? 2 : 0));
  if (domain) (exception ? exceptionRules : wildcard ? wildcardRules : exactRules).add(domain);
}

function publicSuffixLabels(hostname) {
  const host = asciiDomain(hostname);
  if (!host || host.includes(":") || /^\d+(?:\.\d+){3}$/u.test(host)) return 0;
  const labels = host.split(".");
  let match = 1; // PSL prevailing default rule: *
  for (let i = 0; i < labels.length; i += 1) {
    const suffix = labels.slice(i).join(".");
    if (exceptionRules.has(suffix)) return labels.length - i - 1;
    if (exactRules.has(suffix)) match = Math.max(match, labels.length - i);
    if (i > 0 && wildcardRules.has(suffix)) match = Math.max(match, labels.length - i + 1);
  }
  return match;
}

function isPublicSuffix(candidate) {
  const domain = asciiDomain(candidate);
  const count = publicSuffixLabels(domain);
  return count > 0 && domain.split(".").length === count;
}

function cookieDomainCandidates(hostname) {
  const host = asciiDomain(hostname);
  if (!host) return [];
  const labels = host.split(".");
  const suffixCount = publicSuffixLabels(host);
  if (!suffixCount) return [host];
  if (labels.length <= suffixCount) return [];
  const candidates = [];
  for (let i = 0; i < labels.length - suffixCount; i += 1) candidates.push(labels.slice(i).join("."));
  return candidates;
}

function exactOriginPermission(origin) {
  return `${origin}/*`;
}

async function requirePermissions(origins) {
  const missing = [];
  const required = new Set();
  for (const origin of origins) {
    const url = new URL(origin);
    if (url.protocol !== "https:") throw new Error("Only HTTPS site origins can be selected.");
    for (const domain of cookieDomainCandidates(url.hostname)) {
      const candidate = new URL(url.origin);
      candidate.hostname = domain;
      // Chrome host permission match patterns do not distinguish ports.
      candidate.port = "";
      // Chrome checks cookie host access against the cookie's URL scheme:
      // Secure cookies use HTTPS, while non-Secure cookies use HTTP.
      for (const protocol of ["https:", "http:"]) {
        const schemeCandidate = new URL(candidate.origin);
        schemeCandidate.protocol = protocol;
        required.add(schemeCandidate.origin);
      }
    }
  }
  for (const origin of [...required].sort()) {
    if (!(await chrome.permissions.contains({ origins: [exactOriginPermission(origin)] }))) missing.push(origin);
  }
  if (missing.length) {
    await chrome.storage.local.set({ pending_origins: missing });
    throw Object.assign(new Error("Chrome permission is required for the selected site."), {
      code: "CHROME_PERMISSION_REQUIRED", details: { origins: missing }
    });
  }
}

async function freshSelectedSites(requestedSites, serviceOrigin) {
  if (!Array.isArray(requestedSites) || requestedSites.length < 1 || requestedSites.length > 100) {
    throw new Error("A selection must contain between 1 and 100 sites.");
  }
  const open = await enumerateOpenSites(serviceOrigin);
  const byId = new Map(open.map((site) => [site.site_id, site]));
  const result = [];
  const seen = new Set();
  for (const requested of requestedSites) {
    if (!requested || typeof requested.site_id !== "string" || seen.has(requested.site_id)) continue;
    seen.add(requested.site_id);
    const decoded = siteFromId(requested.site_id);
    const current = byId.get(requested.site_id);
    if (!decoded || !current || decoded.origin !== requested.origin || decoded.store_id !== requested.store_id) {
      throw Object.assign(new Error("A selected site is no longer open in the same Chrome cookie store."), { code: "COOKIE_SELECTION_STALE" });
    }
    result.push(current);
  }
  if (!result.length) throw new Error("No current open sites were selected.");
  return result;
}

async function jobOpenSites(payload, serviceOrigin) {
  if (payload && Object.keys(payload).length) throw new Error("open_sites takes an empty payload.");
  return { sites: await enumerateOpenSites(serviceOrigin) };
}

async function jobCookieCatalog(payload, serviceOrigin) {
  if (!payload || !Array.isArray(payload.sites)) throw new Error("cookie_catalog requires selected sites.");
  const sites = await freshSelectedSites(payload.sites, serviceOrigin);
  await requirePermissions(sites.map((site) => site.origin));
  const records = new Map();
  for (const site of sites) {
    const hostname = new URL(site.origin).hostname;
    for (const domain of cookieDomainCandidates(hostname)) {
      const cookies = await readCookies(domain, site.store_id);
      for (const cookie of cookies) {
        if (!cookieAppliesToHost(cookie, hostname)) continue;
        const identity = cookieIdentity(cookie);
        const old = records.get(identity);
        if (old) old.site_ids.add(site.site_id);
        else records.set(identity, { cookie, site_ids: new Set([site.site_id]) });
      }
    }
  }
  const catalog = [...records.values()].map(({ cookie, site_ids }) => metadataFor(cookie, site_ids)).sort((a, b) =>
    a.domain.localeCompare(b.domain) || a.path.localeCompare(b.path) || a.name.localeCompare(b.name) || a.store_id.localeCompare(b.store_id));
  const output = { sites, cookies: catalog };
  const bytes = new TextEncoder().encode(JSON.stringify(output)).length;
  if (bytes > MAX_MESSAGE_BYTES) throw new Error("Cookie catalog exceeds the extension message size limit.");
  await chrome.storage.local.set({ [CATALOG_KEY]: { created_at: Date.now(), sites, cookies: catalog } });
  await chrome.storage.local.remove("pending_origins");
  return output;
}

function comparableAttributes(cookie) {
  return JSON.stringify({
    source_key: cookieIdentity(cookie),
    name: cookie.name,
    domain: cookie.domain,
    path: cookie.path,
    store_id: cookie.storeId,
    host_only: Boolean(cookie.hostOnly),
    secure: Boolean(cookie.secure),
    http_only: Boolean(cookie.httpOnly),
    same_site: cookie.sameSite || "unspecified",
    expires_at: Number.isFinite(cookie.expirationDate) ? cookie.expirationDate : null,
    partition_key: cookie.partitionKey || null
  });
}

function comparableSelection(selection) {
  return JSON.stringify({
    source_key: selection.source_key,
    name: selection.name,
    domain: selection.domain,
    path: selection.path,
    store_id: selection.store_id,
    host_only: selection.host_only,
    secure: selection.secure,
    http_only: selection.http_only,
    same_site: selection.same_site,
    expires_at: selection.expires_at,
    partition_key: selection.partition_key || null
  });
}

async function jobCookieExport(payload, serviceOrigin) {
  if (!payload || !Array.isArray(payload.sites) || !Array.isArray(payload.selections) || payload.selections.length < 1 || payload.selections.length > 500) {
    throw new Error("cookie_export requires 1 to 500 selected catalog cookies and sites.");
  }
  const sites = await freshSelectedSites(payload.sites, serviceOrigin);
  await requirePermissions(sites.map((site) => site.origin));
  const stored = await chrome.storage.local.get(CATALOG_KEY);
  const catalog = stored[CATALOG_KEY];
  if (!catalog || Date.now() - catalog.created_at > 10 * 60_000) {
    throw Object.assign(new Error("Cookie catalog expired; refresh it before export."), { code: "COOKIE_SELECTION_STALE" });
  }
  const selectedSiteIds = new Set(sites.map((site) => site.site_id));
  const knownSites = new Map(catalog.sites.map((site) => [site.site_id, site]));
  for (const site of sites) {
    const known = knownSites.get(site.site_id);
    if (!known || known.origin !== site.origin || known.store_id !== site.store_id) {
      throw Object.assign(new Error("Selected sites changed after the cookie catalog was created."), { code: "COOKIE_SELECTION_STALE" });
    }
  }
  const knownCookies = new Map(catalog.cookies.map((cookie) => [cookie.source_key, cookie]));
  const currentCookies = new Map();
  for (const site of sites) {
    const hostname = new URL(site.origin).hostname;
    for (const domain of cookieDomainCandidates(hostname)) {
      const cookies = await readCookies(domain, site.store_id);
      for (const cookie of cookies) {
        if (cookieAppliesToHost(cookie, hostname)) currentCookies.set(cookieIdentity(cookie), cookie);
      }
    }
  }
  const output = [];
  const outputKeys = new Set();
  for (const selection of payload.selections) {
    const known = selection && knownCookies.get(selection.source_key);
    if (!known || known.unsupported_reason || comparableSelection(selection) !== JSON.stringify({
      source_key: known.source_key, name: known.name, domain: known.domain, path: known.path,
      store_id: known.store_id, host_only: known.host_only, secure: known.secure,
      http_only: known.http_only, same_site: known.same_site, expires_at: known.expires_at,
      partition_key: known.partition_key || null
    })) {
      throw Object.assign(new Error("A selected cookie is not an eligible item from the current catalog."), { code: "COOKIE_SELECTION_STALE" });
    }
  if (!known.site_ids.some((id) => selectedSiteIds.has(id)) ||
      JSON.stringify([...(selection.site_ids || [])].sort()) !== JSON.stringify([...known.site_ids].sort())) {
      throw Object.assign(new Error("A selected cookie is outside the selected sites."), { code: "COOKIE_SELECTION_STALE" });
    }
    const cookie = currentCookies.get(known.source_key);
    if (!cookie || comparableAttributes(cookie) !== comparableSelection(known)) {
      throw Object.assign(new Error("A selected cookie was removed or its attributes changed."), { code: "COOKIE_SELECTION_STALE" });
    }
    if (outputKeys.has(known.source_key)) continue;
    outputKeys.add(known.source_key);
    output.push({ ...known, value: cookie.value });
  }
  const result = { cookies: output, allowed_origins: sites.map((site) => site.origin) };
  if (new TextEncoder().encode(JSON.stringify(result)).length > MAX_MESSAGE_BYTES) throw new Error("Cookie export exceeds the extension message size limit.");
  return result;
}

function taskCookieOrigins(payload, serviceOrigin) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
      Object.keys(payload).some((key) => key !== "origins") ||
      !Array.isArray(payload.origins) || payload.origins.length < 1 || payload.origins.length > 10) {
    throw new Error("task_cookie_export requires 1 to 10 allowed site origins.");
  }
  const seen = new Set();
  const origins = [];
  for (const value of payload.origins) {
    if (typeof value !== "string" || value.length > 2048) throw new Error("task_cookie_export contains an invalid site origin.");
    let url;
    try { url = new URL(value); } catch { throw new Error("task_cookie_export contains an invalid site origin."); }
    if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash ||
        url.origin !== value || value === serviceOrigin || seen.has(value)) {
      throw new Error("task_cookie_export contains an invalid or duplicate site origin.");
    }
    seen.add(value);
    origins.push(value);
  }
  return origins;
}

async function normalCookieStoreId() {
  let tabs, stores;
  try {
    [tabs, stores] = await Promise.all([
      chrome.tabs.query({ windowType: "normal" }),
      chrome.cookies.getAllCookieStores()
    ]);
  } catch {
    throw Object.assign(new Error("Chrome's normal cookie store could not be identified."), { code: "COOKIE_SOURCE_AMBIGUOUS" });
  }
  const normalTabIds = new Set((Array.isArray(tabs) ? tabs : [])
    .filter((tab) => tab && tab.incognito !== true && Number.isInteger(tab.id))
    .map((tab) => tab.id));
  const normalStoreIds = new Set();
  for (const store of Array.isArray(stores) ? stores : []) {
    if (typeof store?.id !== "string" || !Array.isArray(store.tabIds)) continue;
    if (store.tabIds.some((tabId) => normalTabIds.has(tabId))) normalStoreIds.add(store.id);
  }
  if (normalStoreIds.size !== 1) {
    throw Object.assign(new Error("Keep a normal Chrome tab open in the paired profile so its cookie store can be identified."), { code: "COOKIE_SOURCE_AMBIGUOUS" });
  }
  return [...normalStoreIds][0];
}

async function jobTaskCookieExport(payload, serviceOrigin) {
  const origins = taskCookieOrigins(payload, serviceOrigin);
  await requirePermissions(origins);
  const storeId = await normalCookieStoreId();
  const records = new Map();
  for (const origin of origins) {
    const hostname = new URL(origin).hostname;
    for (const domain of cookieDomainCandidates(hostname)) {
      let cookies;
      try { cookies = await readCookies(domain, storeId); }
      catch { throw Object.assign(new Error("Chrome could not read all cookies for the allowed site."), { code: "COOKIE_SYNC_FAILED" }); }
      for (const cookie of cookies) {
        if (!cookieAppliesToHost(cookie, hostname)) continue;
        if (cookie.partitionKey) {
          throw Object.assign(new Error("A partitioned cookie for an allowed site cannot be transferred safely."), { code: "COOKIE_SYNC_UNSUPPORTED" });
        }
        const row = metadataFor(cookie, []);
        if (row.unsupported_reason) {
          throw Object.assign(new Error("A cookie for an allowed site cannot be transferred safely."), { code: "COOKIE_SYNC_UNSUPPORTED" });
        }
        if (typeof cookie.name !== "string" || typeof cookie.value !== "string" || !cookie.name ||
            cookie.name.length > 4096 || cookie.value.length > 16_384) {
          throw Object.assign(new Error("A cookie for an allowed site has unsupported attributes."), { code: "COOKIE_SYNC_UNSUPPORTED" });
        }
        const key = cookieIdentity(cookie);
        if (!records.has(key)) records.set(key, cookie);
        if (records.size > MAX_TASK_COOKIE_COUNT) {
          throw Object.assign(new Error("The allowed sites contain too many cookies to transfer safely."), { code: "COOKIE_SYNC_FAILED" });
        }
      }
    }
  }
  const cookies = [...records.values()].map((cookie) => ({
    ...metadataFor(cookie, []),
    value: cookie.value
  }));
  const result = { allowed_origins: origins, cookies };
  if (new TextEncoder().encode(JSON.stringify(result)).length > MAX_TASK_COOKIE_BYTES) {
    throw Object.assign(new Error("The cookie export exceeds the safe transfer size."), { code: "COOKIE_SYNC_FAILED" });
  }
  return result;
}

async function handleJob(message, jobSocket) {
  const requestId = message && message.request_id;
  if (typeof requestId !== "string" || requestId.length < 1 || requestId.length > 200) return;
  let response;
  try {
    if (new TextEncoder().encode(JSON.stringify(message)).length > MAX_MESSAGE_BYTES) throw new Error("Job exceeds the extension message size limit.");
    const state = await getState();
    if (!state.source_id || !state.token) throw new Error("Extension is not paired.");
    const origin = state.api_origin;
    switch (message.kind) {
      case "open_sites":
        response = { type: "result", request_id: requestId, ok: true, data: await jobOpenSites(message.payload || {}, origin) };
        break;
      case "cookie_catalog":
        response = { type: "result", request_id: requestId, ok: true, data: await jobCookieCatalog(message.payload, origin) };
        break;
      case "cookie_export":
        response = { type: "result", request_id: requestId, ok: true, data: await jobCookieExport(message.payload, origin) };
        break;
      case "task_cookie_export":
        response = { type: "result", request_id: requestId, ok: true, data: await jobTaskCookieExport(message.payload, origin) };
        break;
      default:
        throw Object.assign(new Error("Unsupported extension job kind."), { code: "UNSUPPORTED_JOB" });
    }
  } catch (error) {
    response = {
      type: "result", request_id: requestId, ok: false,
      error: {
        code: error?.code || "EXTENSION_JOB_FAILED",
        message: String(error?.message || "Extension job failed.").slice(0, 240),
        ...(error?.details ? { details: error.details } : {})
      }
    };
  }
  if (socket === jobSocket && jobSocket?.readyState === WebSocket.OPEN) {
    const encoded = JSON.stringify(response);
    if (new TextEncoder().encode(encoded).length <= MAX_MESSAGE_BYTES) socket.send(encoded);
  }
}

function stopHeartbeat() {
  if (heartbeatTimer !== null) clearTimeout(heartbeatTimer);
  heartbeatTimer = null;
}

function scheduleHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setTimeout(() => {
    heartbeatTimer = null;
    if (socket?.readyState !== WebSocket.OPEN) return;
    try {
      socket.send(JSON.stringify({ type: "heartbeat" }));
      scheduleHeartbeat();
    } catch {
      socket.close();
    }
  }, HEARTBEAT_MS);
}

async function connect() {
  if (reconnecting || (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING))) return;
  reconnecting = true;
  try {
    const state = await getState();
    if (!state.source_id || !state.token) {
      await setStatus("unpaired", "Enter a pairing code in the extension popup.");
      return;
    }
    const wsUrl = state.api_origin.replace(/^http:/u, "ws:") + "/api/v1/extension/channel";
    const ws = new WebSocket(wsUrl);
    socket = ws;
    ws.addEventListener("open", () => {
      if (socket !== ws) return;
      ws.send(JSON.stringify({ type: "auth", source_id: state.source_id, token: state.token }));
      scheduleHeartbeat();
      setStatus("connected");
    });
    ws.addEventListener("message", (event) => {
      if (socket !== ws || typeof event.data !== "string" || new TextEncoder().encode(event.data).length > MAX_MESSAGE_BYTES) return;
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message?.type === "job") void handleJob(message, ws);
    });
    ws.addEventListener("error", () => setStatus("offline", "Could not reach the local service."));
    ws.addEventListener("close", () => {
      if (socket === ws) socket = null;
      stopHeartbeat();
      void setStatus("offline", "The local service connection closed.");
    });
  } catch {
    await setStatus("offline", "Could not connect to the local service.");
  } finally {
    reconnecting = false;
  }
}

if (typeof chrome !== "undefined") {
  chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 0.5 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === RECONNECT_ALARM) void connect();
  });
  chrome.runtime.onStartup.addListener(() => void connect());
  chrome.runtime.onInstalled.addListener(() => void connect());
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.type !== "string") return false;
  if (message.type === "pair") {
    void pair(message).then(sendResponse, (error) => sendResponse({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message.type === "get_status") {
    void Promise.all([getState(), chrome.storage.local.get(STATE_KEY)]).then(([connection, stored]) => sendResponse({
      ok: true, connection, status: stored[STATE_KEY] || { status: "offline" }
    }));
    return true;
  }
  if (message.type === "grant_pending") {
    void chrome.storage.local.remove("pending_origins").then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === "connect_now") {
    void connect().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message.type === "open_panel") {
    void getState().then((state) => chrome.tabs.create({ url: state.api_origin })).then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
  });
}

async function pair(message) {
  const origin = String(message.api_origin || DEFAULT_API_ORIGIN).trim().replace(/\/$/u, "");
  if (!validApiOrigin(origin)) return { ok: false, error: "API origin must be an HTTP localhost or 127.0.0.1 origin with a valid port." };
  const code = String(message.code || "").trim();
  const label = String(message.label || "My Chrome").trim().slice(0, 80);
  if (!code || code.length > 200) return { ok: false, error: "Enter a valid pairing code." };
  try {
    const response = await fetch(`${origin}/api/v1/extension/pair`, {
      method: "POST", credentials: "omit", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, label, extension_id: chrome.runtime.id })
    });
    if (!response.ok) return { ok: false, error: `Pairing failed (${response.status}).` };
    const paired = await response.json();
    if (typeof paired.source_id !== "string" || typeof paired.token !== "string" || paired.token.length < 16 || paired.token.length > 4096) {
      return { ok: false, error: "Local service returned an invalid pairing response." };
    }
    if (socket) {
      stopHeartbeat();
      socket.close();
      socket = null;
    }
    await chrome.storage.local.set({ api_origin: origin, source_id: paired.source_id, token: paired.token, label, pending_origins: [] });
    await connect();
    return { ok: true };
  } catch {
    return { ok: false, error: "Could not pair with the local service." };
  }
}

export {
  cookieAppliesToHost,
  cookieDomainCandidates,
  cookieIdentity,
  enumerateOpenSites,
  jobCookieCatalog,
  jobCookieExport,
  jobTaskCookieExport,
  jobOpenSites,
  requirePermissions
};

