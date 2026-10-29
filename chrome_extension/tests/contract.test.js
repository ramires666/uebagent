import assert from "node:assert/strict";
import test from "node:test";

const state = {
  tabs: [],
  stores: [],
  cookies: [],
  grantedOrigins: new Set(),
  permissionChecks: [],
  tabQueries: [],
  cookieQueries: [],
  failedDomains: new Set(),
  storage: {}
};

function eventHook() {
  return { addListener() {} };
}

function canonicalDomain(value) {
  return String(value || "").replace(/^\./u, "").toLowerCase();
}

function cookieMatchesFilter(cookie, filterDomain) {
  const cookieDomain = canonicalDomain(cookie.domain);
  const query = canonicalDomain(filterDomain);
  return cookieDomain === query || cookieDomain.endsWith(`.${query}`);
}

globalThis.chrome = {
  runtime: { id: "test-extension-id", onStartup: eventHook(), onInstalled: eventHook(), onMessage: eventHook() },
  alarms: { create() {}, onAlarm: eventHook() },
  tabs: { async query(options) { state.tabQueries.push(options); return state.tabs; } },
  cookies: {
    async getAllCookieStores() { return state.stores; },
    async getAll({ domain, storeId }) {
      state.cookieQueries.push({ domain, storeId });
      if (state.failedDomains.has(domain)) throw new Error(`private error for https://${domain}/secret`);
      return state.cookies.filter((cookie) => cookie.storeId === storeId && cookieMatchesFilter(cookie, domain));
    }
  },
  permissions: {
    async contains({ origins }) {
      state.permissionChecks.push(...origins);
      return origins.every((origin) => state.grantedOrigins.has(origin));
    }
  },
  storage: {
    local: {
      async get(keys) {
        if (Array.isArray(keys)) return Object.fromEntries(keys.map((key) => [key, state.storage[key]]).filter(([, value]) => value !== undefined));
        if (typeof keys === "string") return { [keys]: state.storage[keys] };
        return { ...state.storage };
      },
      async set(values) { Object.assign(state.storage, values); },
      async remove(keys) { for (const key of Array.isArray(keys) ? keys : [keys]) delete state.storage[key]; }
    }
  }
};

const {
  cookieDomainCandidates,
  cookieIdentity,
  enumerateOpenSites,
  jobCookieCatalog,
  jobCookieExport,
  jobTaskCookieExport,
  switchApiOrigin
} = await import("../service_worker.js");

const serviceOrigin = "http://localhost:8080";

function reset() {
  state.tabs = [];
  state.stores = [];
  state.cookies = [];
  state.grantedOrigins = new Set();
  state.permissionChecks = [];
  state.tabQueries = [];
  state.cookieQueries = [];
  state.failedDomains = new Set();
  state.storage = {};
}

test("explicit service move changes only the fixed loopback origin and retains pair credentials", async () => {
  reset();
  state.storage = {
    api_origin: "http://localhost:8080",
    source_id: "paired-source-fixture",
    token: "paired-token-fixture-123456",
    label: "Fixture Chrome",
    pending_origins: ["https://example.test"]
  };
  const result = await switchApiOrigin();
  assert.deepEqual(result, { ok: true });
  assert.equal(state.storage.api_origin, "http://localhost:8090");
  assert.equal(state.storage.source_id, "paired-source-fixture");
  assert.equal(state.storage.token, "paired-token-fixture-123456");
  assert.equal(state.storage.label, "Fixture Chrome");
  assert.deepEqual(state.storage.pending_origins, ["https://example.test"]);
});

test("service move requires an existing pairing and does not change storage otherwise", async () => {
  reset();
  state.storage = { api_origin: "http://localhost:8080" };
  const before = { ...state.storage };
  const result = await switchApiOrigin();
  assert.equal(result.ok, false);
  assert.deepEqual(state.storage, before);
});

function tab(id, url, options = {}) {
  return { id, url, incognito: false, ...options };
}

function site(tabId, url, storeId = "store-normal") {
  state.tabs.push(tab(tabId, url));
  let store = state.stores.find((row) => row.id === storeId);
  if (!store) state.stores.push(store = { id: storeId, tabIds: [] });
  store.tabIds.push(tabId);
}

function cookie(overrides = {}) {
  return {
    storeId: "store-normal",
    name: "sid",
    value: "initial-secret-value",
    domain: "app.example.com",
    path: "/",
    hostOnly: true,
    secure: true,
    httpOnly: true,
    sameSite: "lax",
    ...overrides
  };
}

function grantTaskCookiePermissions(origins) {
  for (const origin of origins) {
    const url = new URL(origin);
    for (const domain of cookieDomainCandidates(url.hostname)) {
      state.grantedOrigins.add(`https://${domain}/*`);
      state.grantedOrigins.add(`http://${domain}/*`);
    }
  }
}

async function catalogOneSite() {
  const sites = await enumerateOpenSites(serviceOrigin);
  for (const origin of [
    "http://app.example.com/*", "http://example.com/*",
    "https://app.example.com/*", "https://example.com/*"
  ]) state.grantedOrigins.add(origin);
  const catalog = await jobCookieCatalog({ sites }, serviceOrigin);
  return { sites, catalog };
}

function errorCode(error) {
  return error?.code;
}

test("open_sites groups normal HTTPS tabs by origin and store and excludes incognito and service tabs", async () => {
  reset();
  site(1, "https://shop.example.com/products?secret=1");
  site(2, "https://shop.example.com/cart#frag");
  site(3, "https://shop.example.com:8443/");
  state.stores.push({ id: "store-private", tabIds: [4] });
  state.tabs.push(tab(4, "https://private.example.com/", { incognito: true }));
  site(5, "https://service.example.test/panel");
  site(6, "chrome://settings/");

  const sites = await enumerateOpenSites("https://service.example.test");
  assert.deepEqual(sites.map(({ origin, store_id, tab_count }) => ({ origin, store_id, tab_count })), [
    { origin: "https://shop.example.com", store_id: "store-normal", tab_count: 2 },
    { origin: "https://shop.example.com:8443", store_id: "store-normal", tab_count: 1 }
  ]);
  assert.ok(sites.every((row) => !("url" in row) && !("title" in row)));
});

test("parent-domain candidates stop at known public suffixes without querying a full jar", () => {
  assert.deepEqual(cookieDomainCandidates("app.example.com"), ["app.example.com", "example.com"]);
  assert.deepEqual(cookieDomainCandidates("a.shop.example.co.uk"), ["a.shop.example.co.uk", "shop.example.co.uk", "example.co.uk"]);
  assert.ok(!cookieDomainCandidates("app.example.co.uk").includes("co.uk"));
  assert.deepEqual(cookieDomainCandidates("x.shop.example.com.au"), ["x.shop.example.com.au", "shop.example.com.au", "example.com.au"]);
  assert.deepEqual(cookieDomainCandidates("x.user.github.io"), ["x.user.github.io", "user.github.io"]);
  assert.deepEqual(cookieDomainCandidates("x.www.ck"), ["x.www.ck", "www.ck"]); // exception !www.ck
  assert.deepEqual(cookieDomainCandidates("x.a.ck"), ["x.a.ck"]); // wildcard *.ck
  assert.deepEqual(cookieDomainCandidates("github.io"), []);
  assert.deepEqual(cookieDomainCandidates("www.\u98df\u72ee.\u516c\u53f8.cn"), ["www.xn--85x722f.xn--55qx5d.cn", "xn--85x722f.xn--55qx5d.cn"]);

});

test("catalog collects applicable parent cookies across paths, distinguishes identity, and omits values", async () => {
  reset();
  site(1, "https://app.example.com/account/home");
  state.cookies = [
    cookie({ name: "sid", value: "parent-root", domain: ".example.com", hostOnly: false, path: "/" }),
    cookie({ name: "sid", value: "parent-account", domain: ".example.com", hostOnly: false, path: "/account" }),
    cookie({ name: "sid", value: "host-root", domain: "app.example.com", hostOnly: true, path: "/" }),
    cookie({ name: "sibling-secret", value: "must-not-appear", domain: "other.example.com", hostOnly: true, path: "/" }),
    cookie({ name: "suffix-trick", value: "must-not-appear-either", domain: "example.com.evil.test", hostOnly: false, path: "/" })
  ];

  const { sites, catalog } = await catalogOneSite();
  assert.equal(catalog.cookies.length, 3);
  assert.deepEqual(catalog.cookies.map(({ path }) => path).sort(), ["/", "/", "/account"]);
  assert.equal(new Set(catalog.cookies.map(({ source_key }) => source_key)).size, 3);
  assert.ok(catalog.cookies.every((row) => !("value" in row)));
  assert.ok(!JSON.stringify(catalog).includes("must-not-appear"));
  assert.ok(!JSON.stringify(catalog).includes("must-not-appear-either"));
  assert.ok(state.cookieQueries.some(({ domain }) => domain === "example.com"));
  assert.deepEqual(state.permissionChecks, [
    "http://app.example.com/*", "http://example.com/*",
    "https://app.example.com/*", "https://example.com/*"
  ]);
  assert.deepEqual(catalog.sites, sites);
  assert.notEqual(cookieIdentity(state.cookies[0]), cookieIdentity(state.cookies[1]));
});

test("export returns only exact selected identity with freshly rotated value", async () => {
  reset();
  site(1, "https://app.example.com/account");
  state.cookies = [
    cookie({ name: "sid", value: "catalog-value", domain: ".example.com", hostOnly: false, path: "/" }),
    cookie({ name: "csrf", value: "unselected-secret", domain: ".example.com", hostOnly: false, path: "/" })
  ];
  const { sites, catalog } = await catalogOneSite();
  const selected = catalog.cookies.find((row) => row.name === "sid");
  state.cookies[0] = { ...state.cookies[0], value: "rotated-value" };

  const result = await jobCookieExport({ sites, selections: [selected] }, serviceOrigin);
  assert.deepEqual(result.allowed_origins, ["https://app.example.com"]);
  assert.equal(result.cookies.length, 1);
  assert.equal(result.cookies[0].source_key, selected.source_key);
  assert.equal(result.cookies[0].value, "rotated-value");
  assert.ok(!JSON.stringify(result).includes("unselected-secret"));
});

test("export rejects a selected site after its last tab closes", async () => {
  reset();
  site(1, "https://app.example.com/");
  state.cookies = [cookie()];
  const { sites, catalog } = await catalogOneSite();
  state.tabs = [];
  await assert.rejects(jobCookieExport({ sites, selections: [catalog.cookies[0]] }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SELECTION_STALE");
});

test("export rejects a deleted cookie and a foreign or unknown source_key", async (t) => {
  reset();
  site(1, "https://app.example.com/");
  state.cookies = [cookie()];
  const { sites, catalog } = await catalogOneSite();
  const selected = catalog.cookies[0];

  await t.test("deleted identity", async () => {
    state.cookies = [];
    await assert.rejects(jobCookieExport({ sites, selections: [selected] }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SELECTION_STALE");
  });
  await t.test("unknown source key", async () => {
    state.cookies = [cookie()];
    await assert.rejects(jobCookieExport({ sites, selections: [{ ...selected, source_key: "foreign-key" }] }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SELECTION_STALE");
  });
  await t.test("identity from a different cookie store", async () => {
    state.cookies = [cookie()];
    const foreign = cookieIdentity(cookie({ storeId: "store-other" }));
    await assert.rejects(jobCookieExport({ sites, selections: [{ ...selected, source_key: foreign }] }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SELECTION_STALE");
  });
  await t.test("cookie cannot be smuggled from another site's catalog", async () => {
    await assert.rejects(jobCookieExport({ sites, selections: [{ ...selected, site_ids: ["foreign-site"] }] }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SELECTION_STALE");
  });
});

test("catalog reports exact missing host permission origins in error.details", async () => {
  reset();
  site(1, "https://app.example.com/");
  const sites = await enumerateOpenSites(serviceOrigin);
  await assert.rejects(jobCookieCatalog({ sites }, serviceOrigin), (error) => {
    assert.equal(error.code, "CHROME_PERMISSION_REQUIRED");
    assert.deepEqual(error.details, { origins: [
      "http://app.example.com", "http://example.com", "https://app.example.com", "https://example.com"
    ] });
    assert.deepEqual(state.storage.pending_origins, error.details.origins);
    return true;
  });
  assert.deepEqual(state.permissionChecks, [
    "http://app.example.com/*", "http://example.com/*",
    "https://app.example.com/*", "https://example.com/*"
  ]);
});

test("permission patterns omit ports and never request a public suffix", async () => {
  reset();
  site(1, "https://app.example.co.uk:8443/");
  const sites = await enumerateOpenSites(serviceOrigin);
  await assert.rejects(jobCookieCatalog({ sites }, serviceOrigin), (error) => {
    assert.deepEqual(error.details.origins, [
      "http://app.example.co.uk", "http://example.co.uk",
      "https://app.example.co.uk", "https://example.co.uk"
    ]);
    return true;
  });
  assert.deepEqual(state.permissionChecks, [
    "http://app.example.co.uk/*", "http://example.co.uk/*",
    "https://app.example.co.uk/*", "https://example.co.uk/*"
  ]);
});

test("HTTPS-only legacy grants fail closed until exact HTTP host permissions are granted", async () => {
  reset();
  site(1, "https://app.example.com/account");
  state.cookies = [
    cookie({ name: "secure-session", value: "synthetic-secure", secure: true }),
    cookie({ name: "SID", value: "synthetic-insecure", secure: false, httpOnly: false })
  ];
  const sites = await enumerateOpenSites(serviceOrigin);
  state.grantedOrigins.add("https://app.example.com/*");
  state.grantedOrigins.add("https://example.com/*");

  await assert.rejects(jobCookieCatalog({ sites }, serviceOrigin), (error) => {
    assert.equal(error.code, "CHROME_PERMISSION_REQUIRED");
    assert.deepEqual(error.details.origins, ["http://app.example.com", "http://example.com"]);
    return true;
  });
  assert.deepEqual(state.cookieQueries, []); // No partial catalog from HTTPS grants alone.

  state.grantedOrigins.add("http://app.example.com/*");
  state.grantedOrigins.add("http://example.com/*");
  const catalog = await jobCookieCatalog({ sites }, serviceOrigin);
  const cookieFlags = catalog.cookies.map(({ name, secure }) => ({ name, secure })).sort((a, b) => a.name < b.name ? -1 : 1);
  assert.deepEqual(cookieFlags, [
    { name: "SID", secure: false }, { name: "secure-session", secure: true }
  ]);
  assert.ok(catalog.cookies.every((row) => !("value" in row)));
  const sid = catalog.cookies.find(({ name }) => name === "SID");
  const exported = await jobCookieExport({ sites, selections: [sid] }, serviceOrigin);
  assert.deepEqual(exported.cookies.map(({ name, value }) => ({ name, value })), [
    { name: "SID", value: "synthetic-insecure" }
  ]);
});

test("catalog and export fail closed when any requested domain cannot be read", async (t) => {
  for (const domain of ["app.example.com", "example.com"]) {
    await t.test(`catalog: ${domain}`, async () => {
      reset();
      site(1, "https://app.example.com/");
      state.cookies = [cookie()];
      state.failedDomains.add(domain);
      await assert.rejects(catalogOneSite(), (error) => {
        assert.equal(error.code, "CHROME_COOKIE_READ_FAILED");
        assert.ok(!error.message.includes("secret"));
        assert.ok(!error.message.includes(domain));
        return true;
      });
      assert.equal(state.storage.current_cookie_catalog, undefined);
    });
    await t.test(`export: ${domain}`, async () => {
      reset();
      site(1, "https://app.example.com/");
      state.cookies = [cookie()];
      const { sites, catalog } = await catalogOneSite();
      state.failedDomains.add(domain);
      await assert.rejects(jobCookieExport({ sites, selections: [catalog.cookies[0]] }, serviceOrigin), (error) => {
        assert.equal(error.code, "CHROME_COOKIE_READ_FAILED");
        assert.ok(!error.message.includes("secret"));
        assert.ok(!error.message.includes(domain));
        return true;
      });
    });
  }
});

test("task cookie export reads a closed task site from the evidenced normal store and excludes siblings and incognito", async () => {
  reset();
  const origins = ["https://shop.example.com", "https://example.com"];
  state.tabs = [tab(90, "https://unrelated.example.net/"), tab(91, "https://private.example.com/", { incognito: true })];
  state.stores = [{ id: "store-normal", tabIds: [90] }, { id: "store-private", tabIds: [91] }];
  state.cookies = [
    cookie({ name: "parent", value: "synthetic-parent-secret", domain: ".example.com", hostOnly: false, storeId: "store-normal" }),
    cookie({ name: "shop-host", value: "synthetic-shop-secret", domain: "shop.example.com", hostOnly: true, secure: false, storeId: "store-normal" }),
    cookie({ name: "root-host", value: "synthetic-root-secret", domain: "example.com", hostOnly: true, storeId: "store-normal" }),
    cookie({ name: "sibling", value: "must-not-export", domain: "other.example.com", hostOnly: true, storeId: "store-normal" }),
    cookie({ name: "private", value: "must-not-export-incognito", domain: ".example.com", hostOnly: false, storeId: "store-private" })
  ];
  grantTaskCookiePermissions(origins);

  const result = await jobTaskCookieExport({ origins }, serviceOrigin);
  assert.deepEqual(result.allowed_origins, origins);
  assert.equal(result.cookies.length, 3);
  const parent = result.cookies.find((row) => row.name === "parent");
  assert.equal(parent.value, "synthetic-parent-secret");
  assert.deepEqual(parent.site_ids, []);
  assert.equal(parent.host_only, false);
  assert.equal(result.cookies.find((row) => row.name === "shop-host").secure, false);
  assert.equal(result.cookies.find((row) => row.name === "shop-host").value, "synthetic-shop-secret");
  assert.ok(result.cookies.some((row) => row.name === "root-host"));
  assert.ok(!JSON.stringify(result).includes("must-not-export"));
  assert.deepEqual(state.tabQueries, [{ windowType: "normal" }]);
  assert.ok(state.cookieQueries.length > 0);
  assert.ok(state.cookieQueries.every(({ storeId }) => storeId === "store-normal"));
  assert.deepEqual(state.storage, {}); // Task cookie values are never persisted by the extension.
});

test("task cookie export requires exact HTTP and HTTPS permissions before reading cookies", async () => {
  reset();
  const origins = ["https://shop.example.com"];
  await assert.rejects(jobTaskCookieExport({ origins }, serviceOrigin), (error) => {
    assert.equal(error.code, "CHROME_PERMISSION_REQUIRED");
    assert.deepEqual(error.details.origins, [
      "http://example.com", "http://shop.example.com", "https://example.com", "https://shop.example.com"
    ]);
    return true;
  });
  assert.deepEqual(state.permissionChecks, [
    "http://example.com/*", "http://shop.example.com/*", "https://example.com/*", "https://shop.example.com/*"
  ]);
  assert.deepEqual(state.cookieQueries, []);
  assert.deepEqual(state.tabQueries, []);
});

test("task cookie export fails closed for ambiguous or incognito-only store evidence", async (t) => {
  for (const scenario of ["multiple-normal", "incognito-only"]) {
    await t.test(scenario, async () => {
      reset();
      const origins = ["https://shop.example.com"];
      grantTaskCookiePermissions(origins);
      if (scenario === "multiple-normal") {
        state.tabs = [tab(1, "https://one.example.test/"), tab(2, "https://two.example.test/")];
        state.stores = [{ id: "store-one", tabIds: [1] }, { id: "store-two", tabIds: [2] }];
      } else {
        state.tabs = [tab(3, "https://private.example.test/", { incognito: true })];
        state.stores = [{ id: "store-private", tabIds: [3] }];
      }
      state.cookies = [cookie({ value: "must-not-read" })];
      await assert.rejects(jobTaskCookieExport({ origins }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SOURCE_AMBIGUOUS");
      assert.deepEqual(state.cookieQueries, []);
    });
  }
});

test("task cookie export rejects partitioned cookies without returning a partial jar", async () => {
  reset();
  const origins = ["https://shop.example.com"];
  state.tabs = [tab(1, "https://unrelated.example.test/")];
  state.stores = [{ id: "store-normal", tabIds: [1] }];
  grantTaskCookiePermissions(origins);
  state.cookies = [
    cookie({ name: "ordinary", value: "synthetic-normal", domain: ".example.com", hostOnly: false }),
    cookie({ name: "partitioned", value: "synthetic-partitioned", domain: ".example.com", hostOnly: false, partitionKey: { topLevelSite: "https://other.example" } })
  ];
  await assert.rejects(jobTaskCookieExport({ origins }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SYNC_UNSUPPORTED");
  assert.deepEqual(state.storage, {});
});

test("task cookie export fails the whole job rather than truncating at count or byte limits", async (t) => {
  for (const scenario of ["cookie-count", "message-size"]) {
    await t.test(scenario, async () => {
      reset();
      const origins = ["https://shop.example.com"];
      state.tabs = [tab(1, "https://unrelated.example.test/")];
      state.stores = [{ id: "store-normal", tabIds: [1] }];
      grantTaskCookiePermissions(origins);
      const count = scenario === "cookie-count" ? 501 : 80;
      const value = scenario === "cookie-count" ? "short-value" : "x".repeat(16_000);
      state.cookies = Array.from({ length: count }, (_, index) => cookie({
        name: `sid${index}`, value, domain: "shop.example.com", hostOnly: true
      }));
      await assert.rejects(jobTaskCookieExport({ origins }, serviceOrigin), (error) => errorCode(error) === "COOKIE_SYNC_FAILED");
    });
  }
});
