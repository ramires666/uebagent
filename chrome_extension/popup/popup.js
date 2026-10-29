const $ = (id) => document.getElementById(id);
let pendingOrigins = [];
const PUBLIC_SERVICE_ORIGIN = "http://localhost:8090";

function send(message) {
  return new Promise((resolve) => chrome.runtime.sendMessage(message, resolve));
}

function showMessage(message, bad = false) {
  $("message").textContent = message || "";
  $("message").style.color = bad ? "#b3261e" : "#137333";
}

$("extension-version").textContent = `Extension ${chrome.runtime.getManifest().version}`;

async function refresh() {
  const response = await send({ type: "get_status" });
  if (!response?.ok) return;
  const { connection, status } = response;
  $("api-origin").value = connection.api_origin;
  $("source-label").value = connection.label;
  $("switch-service-button").hidden = !connection.source_id || connection.api_origin === PUBLIC_SERVICE_ORIGIN;
  $("status").textContent = connection.source_id
    ? `${status.status || "offline"}${status.status_message ? ` — ${status.status_message}` : ""}`
    : "Not paired";
  const origins = (connection.pending_origins || []).filter((origin) => {
    try { const url = new URL(origin); return (url.protocol === "https:" || url.protocol === "http:") && url.origin === origin && !url.username && !url.password; }
    catch { return false; }
  });
  pendingOrigins = origins;
  $("permission-section").hidden = !origins.length;
  $("permission-list").textContent = origins.join("\n");
}

$("pair-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage("Pairing…");
  const response = await send({
    type: "pair",
    api_origin: $("api-origin").value,
    code: $("pairing-code").value,
    label: $("source-label").value
  });
  showMessage(response?.ok ? "Paired. Connecting…" : (response?.error || "Pairing failed."), !response?.ok);
  if (response?.ok) $("pairing-code").value = "";
  await refresh();
});

$("grant-button").addEventListener("click", async () => {
  // Invoke request before any await to retain Chrome's transient user gesture.
  const origins = pendingOrigins;
  let accepted = false;
  try {
    accepted = origins.length > 0 && await chrome.permissions.request({
      origins: origins.map((origin) => `${origin}/*`)
    });
  } catch {
    accepted = false;
  }
  if (accepted) await send({ type: "grant_pending" });
  showMessage(accepted ? "Access granted. Retry the cookie selection in the web panel." : "Chrome did not grant the selected site permissions.", !accepted);
  await refresh();
});

$("connect-button").addEventListener("click", async () => {
  const response = await send({ type: "connect_now" });
  showMessage(response?.ok ? "Reconnect requested." : "Reconnect failed.", !response?.ok);
  await refresh();
});

$("switch-service-button").addEventListener("click", async () => {
  const response = await send({ type: "switch_to_public_service" });
  showMessage(response?.ok
    ? "Подключение перенесено. Сохранённую пару можно использовать дальше."
    : (response?.error || "Не удалось перенести подключение."), !response?.ok);
  await refresh();
});

$("panel-button").addEventListener("click", async () => {
  const response = await send({ type: "open_panel" });
  if (!response?.ok) showMessage("Could not open the local panel.", true);
});

void refresh();
