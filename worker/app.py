"""Local authenticated browser worker. Browser calls are typed and fenced."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from worker.readiness_trace import ReadinessActionTrace
from worker.observation_hints import collect_interaction_hints, enabled as observation_hints_enabled


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "vendor" / "browser-use-camoufox" / "browser_use" / "browser" / "adapter.py"
VIEWPORT = {"width": 1440, "height": 900}
READINESS_ACTION_SNAPSHOT = r"""({x,y}) => {
  const fixture=location.hostname==='readiness-fixture.test';
  const first=(selectors)=>selectors.map(s=>s.startsWith('#')||s.includes(' ')?document.querySelector(s):document.getElementById(s)).find(Boolean)||null;
  const targets={first_name:first(['firstName','first']),last_name:first(['lastName','last']),email:first(['userEmail','email']),age:first(['age']),salary:first(['salary']),department:first(['department'])};
  const desktop=first(['[role="checkbox"][aria-label="Select Desktop"]','.rc-tree-checkbox[aria-label="Select Desktop"]','#desktop-role','#tree-node-desktop-input','tree-node-desktop','desktop','#tree-node-desktop input[type="checkbox"]','#tree-node-desktop [role="checkbox"]']);
  const home=first(['[role="checkbox"][aria-label="Select Home"]','.rc-tree-checkbox[aria-label="Select Home"]','#home-role','#tree-node-home-input','home','#tree-node-home input[type="checkbox"]','#tree-node-home [role="checkbox"]']);
  const homeRow=home?.closest('.rc-tree-treenode,.rct-node')||document.querySelector('.rc-tree-treenode:has([aria-label="Select Home"]),.rct-node:has(#home-role)');
  const desktopRow=desktop?.closest('.rc-tree-treenode,.rct-node')||document.querySelector('.rc-tree-treenode:has([aria-label="Select Desktop"]),.rct-node:has(#desktop-role)');
  const homeNode=first(['tree-node-home']);
  const homeExpander=fixture?null:homeRow?.querySelector('.rc-tree-switcher,.rct-collapse,.rct-collapse-btn')||homeNode?.querySelector('.rct-collapse,.rct-collapse-btn')||null;
  const desktopExpander=fixture?null:desktopRow?.querySelector('.rc-tree-switcher,.rct-collapse,.rct-collapse-btn')||null;
  const fixtureExpander=fixture?document.querySelector('.expander'):null;
  const targetOf=e=>{if(!e)return 'none';
    for(const k of ['first_name','last_name','email','age','salary','department'])if(targets[k]&&(e===targets[k]||targets[k].contains(e)))return k;
    if(home&&(e===home||home.contains(e)))return 'home_checkbox';
    if(desktop&&(e===desktop||desktop.contains(e)))return 'desktop_checkbox';
    if(homeExpander&&(e===homeExpander||homeExpander.contains(e)))return 'home_expander';
    if(desktopExpander&&(e===desktopExpander||desktopExpander.contains(e)))return 'desktop_expander';
    if(fixtureExpander&&(e===fixtureExpander||fixtureExpander.contains(e)))return 'root_expander';
    if(homeRow&&homeRow.contains(e))return 'home_label';
    if(desktopRow&&desktopRow.contains(e))return 'desktop_label';
    if(fixture&&e.closest('.branch'))return 'home_label';
    if(fixture&&e.closest('#desktop'))return 'desktop_checkbox';return 'other';};
  const element=e=>{if(!e)return 'none';if(e===document.body)return 'body';
    const t=(e.getAttribute('type')||'').toLowerCase();if(t==='checkbox'||t==='radio')return 'checkbox';
    const tag=e.tagName.toLowerCase();return ['input','textarea','button','select'].includes(tag)?tag:'other';};
  const focus=document.activeElement;let hit=null,hitCat='none';
  if(Number.isFinite(x)&&Number.isFinite(y)){hit=document.elementFromPoint(x,y);const sem=hit?.closest('.rc-tree-checkbox,[role="checkbox"],input[type="checkbox"],input[type="radio"]')||hit;
    hitCat=!hit?'none':(sem.matches('.rc-tree-checkbox,[role="checkbox"],input[type="checkbox"],input[type="radio"]')?'checkbox':
      (hit.closest('button,[role="button"]')?'button':hit.closest('input')?'input':hit.closest('textarea')?'textarea':hit.closest('select')?'select':hit.closest('a,[role="link"]')?'link':'other'));}
  const t=targetOf(hit);const targetEl=t==='home_checkbox'?home:t==='desktop_checkbox'?desktop:t==='home_expander'?homeExpander:t==='desktop_expander'?desktopExpander:t==='root_expander'?fixtureExpander:targets[t]||null;
  let bbox=null;if(targetEl){const r=targetEl.getBoundingClientRect();bbox={x:r.x,y:r.y,width:r.width,height:r.height};}
  const checked=e=>!e?false:e.checked===true||e.getAttribute('aria-checked')==='true'||e.classList.contains('rc-tree-checkbox-checked')||e.classList.contains('rct-icon-check');
  const checks={home:checked(home),desktop:checked(desktop)};const filled={};for(const k of Object.keys(targets))if(targets[k])filled[k]=!!targets[k].value;
  const bounds={};for(const [k,e] of Object.entries({...targets,home_checkbox:home,desktop_checkbox:desktop,home_expander:homeExpander,desktop_expander:desktopExpander,root_expander:fixtureExpander}))if(e){const r=e.getBoundingClientRect();bounds[k]={x:r.x,y:r.y,width:r.width,height:r.height};}
  return {focus:element(focus),focus_target:targetOf(focus),hit:hitCat,target:t,bbox,bounds,checks,filled};
}"""
LEASE_SECONDS = 5.0
MAX_ACTION_TEXT = 100_000
GEMINI_PROMPT_ACK_TIMEOUT_SECONDS = 2.0
GEMINI_PROMPT_ACK_POLL_SECONDS = 0.1
GEMINI_PROMPT_READY_TIMEOUT_SECONDS = 1.0
GEMINI_PROMPT_READY_POLL_SECONDS = 0.05
GEMINI_PROMPT_EDITOR_TIMEOUT_SECONDS = 5.0
GEMINI_PROMPT_EDITOR_POLL_SECONDS = 0.1
GEMINI_PROMPT_PRE_SUBMIT_TIMEOUT_SECONDS = 2.0
GEMINI_PROMPT_ENTER_TIMEOUT_SECONDS = 2.0
MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
PAGE_CLOSE_TIMEOUT_SECONDS = 2.0
CAPTURE_TIMEOUT_SECONDS = 10.0
CONTEXT_SHUTDOWN_TIMEOUT_SECONDS = 3.0
MAX_PENDING_PAGE_CLOSES = 8
RECOVERY_WATCHDOG_INTERVAL_SECONDS = 1.0
LIVENESS_PROBE_TIMEOUT_SECONDS = 0.5
RECOVERY_START_TIMEOUT_SECONDS = 30.0
RECOVERY_PROCESS_EXIT_TIMEOUT_SECONDS = 3.0
RECOVERY_WINDOW_SECONDS = 300.0
RECOVERY_MAX_ATTEMPTS = 3
RECOVERY_BACKOFF_SECONDS = (2.0, 10.0, 30.0)
OBSERVATION_PRIORITY_WAIT_SECONDS = 1.0
GEMINI_AUTH_API_ORIGINS = frozenset({
    "https://geminiweb-pa.clients6.google.com",
    "https://waa-pa.clients6.google.com",
    "https://signaler-pa.clients6.google.com",
})
GEMINI_AUTH_DIAGNOSTIC_ORIGINS = frozenset({
    "https://accounts.google.com",
    "https://ogs.google.com",
})
_AUTH_PATH_FAMILIES = {
    "/ServiceLogin": "service_login",
    "/RotateCookies": "rotate_cookies",
    "/CheckCookie": "check_cookie",
    "/AccountChooser": "account_chooser",
    "/signin/v2/identifier": "signin_identifier",
}
_RESOURCE_METHOD_FAMILIES = {"GET", "HEAD", "POST", "OPTIONS"}
DEMOQA_INPUT_DIAGNOSTICS = r"""() => {
  if (location.origin !== 'https://demoqa.com' || window !== window.top) return null;
  const inputs = Array.from(document.querySelectorAll('input')).slice(0, 64);
  const count = selector => Math.min(document.querySelectorAll(selector).length, 64);
  const roleCheckboxes = Array.from(document.querySelectorAll('[role="checkbox"]'))
    .filter(element => element.getClientRects().length > 0).slice(0, 64);
  return {
    input_count: Math.min(document.querySelectorAll('input').length, 64),
    truncated: document.querySelectorAll('input').length > 64,
    selector_counts: {
      checkbox_inputs: count('input[type="checkbox"]'),
      desktop_id: count('[id*="desktop"]'),
      desktop_labels: count('label[for*="desktop"]'),
      aria_checkboxes: count('[role="checkbox"]')
    },
    role_checkbox_count: roleCheckboxes.length,
    role_checkboxes_truncated: count('[role="checkbox"]') > 64,
    inputs: inputs.map(input => ({
      tag: input.tagName.toLowerCase().slice(0, 64),
      type: (input.getAttribute('type') || '').slice(0, 64),
      id: (input.id || '').slice(0, 64),
      name: (input.getAttribute('name') || '').slice(0, 64),
      aria_label: (input.getAttribute('aria-label') || '').slice(0, 64),
      pattern: (input.getAttribute('pattern') || '').slice(0, 128),
      checked: input.checked === true,
      validity_valid: input.validity?.valid === true,
      aria_checked: input.getAttribute('aria-checked')?.slice(0, 64) ?? null,
      class_names: Array.from(input.classList).slice(0, 8).map(name => name.slice(0, 64)),
      labels: Array.from(input.labels || []).slice(0, 8).map(label =>
        (label.innerText || label.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 128))
    })),
    role_checkboxes: roleCheckboxes.map(element => {
      const labelledBy = (element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).slice(0, 8);
      const referencedLabel = labelledBy.map(id => document.getElementById(id))
        .filter(Boolean).map(label => label.innerText || label.textContent || '').join(' ');
      const associatedLabel = element.closest('label') || element.closest('.rct-text')?.querySelector('.rct-title');
      const staticLabel = (referencedLabel || associatedLabel?.innerText || associatedLabel?.textContent || '')
        .replace(/\s+/g, ' ').trim().slice(0, 128);
      const ariaChecked = element.getAttribute('aria-checked');
      let selector = null;
      if (/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(element.id || '')) {
        const byId = `[role="checkbox"]#${element.id}`;
        if (document.querySelectorAll(byId).length === 1) selector = byId;
      } else if (/^[A-Za-z0-9 _-]{1,64}$/.test(staticLabel)) {
        const labelledNodes = Array.from(document.querySelectorAll('.rct-node')).filter(node =>
          Array.from(node.querySelectorAll('.rct-title')).some(title =>
            (title.innerText || title.textContent || '').replace(/\s+/g, ' ').trim() === staticLabel));
        const labelledCheckboxes = labelledNodes.flatMap(node => Array.from(node.querySelectorAll('[role="checkbox"]')));
        if (labelledCheckboxes.length === 1 && labelledCheckboxes[0] === element)
          selector = `.rct-node:has(.rct-title:text-is("${staticLabel}")) [role="checkbox"]`;
      }
      return {
        tag: element.tagName.toLowerCase().slice(0, 64),
        id: (element.id || '').slice(0, 64),
        role: (element.getAttribute('role') || '').slice(0, 64),
        aria_label: (element.getAttribute('aria-label') || '').slice(0, 64),
        aria_checked: ariaChecked?.slice(0, 64) ?? null,
        checked: ariaChecked === 'true' ? true : ariaChecked === 'false' ? false : null,
        visible: element.getClientRects().length > 0,
        class_names: Array.from(element.classList).slice(0, 8).map(name => name.slice(0, 64)),
        accessible_label: (element.getAttribute('aria-label') || referencedLabel || staticLabel).slice(0, 128),
        static_label: staticLabel,
        selector
      };
    })
  };
}"""
DEMOQA_FONT_DIAGNOSTICS = r"""() => {
  if (location.origin !== 'https://demoqa.com' || window !== window.top) return null;
  const candidates = [
    { token: 'main-header', nodes: document.querySelectorAll('.main-header') },
    { token: 'main-h1', nodes: document.querySelectorAll('main h1') },
    { token: 'h1', nodes: document.querySelectorAll('h1') }
  ];
  const choice = candidates.find(candidate => candidate.nodes.length === 1);
  const heading = choice ? choice.nodes[0] : null;
  const fontFamily = heading ? getComputedStyle(heading).fontFamily.slice(0, 128) : '';
  const fontFaces = Array.from(document.fonts).slice(0, 16).map(face => ({
    family: (face.family || '').slice(0, 64),
    status: (face.status || '').slice(0, 64)
  }));
  const resourceEntries = performance.getEntriesByType('resource').slice(-128);
  const allResources = resourceEntries.flatMap(entry => {
    try {
      const url = new URL(entry.name, location.href);
      if (!/\.(woff2?|ttf|otf)$/i.test(url.pathname)) return [];
      return [{ origin: url.origin.slice(0, 128), path: url.pathname.slice(0, 128) }];
    } catch (_) { return []; }
  });
  return {
    heading_selector: choice?.token || 'none',
    heading_match_count: choice ? Math.min(choice.nodes.length, 64) : 0,
    header_text: (heading?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 64),
    font_family: fontFamily,
    fonts_status: (document.fonts.status || '').slice(0, 64),
    fonts_check: !!fontFamily && document.fonts.check(`16px ${fontFamily}`),
    font_faces_count: Math.min(document.fonts.size, 16),
    font_faces_truncated: document.fonts.size > 16,
    font_faces: fontFaces,
    font_resources_count: Math.min(allResources.length, 16),
    font_resources_truncated: allResources.length > 16 || resourceEntries.length === 128,
    font_resources: allResources.slice(0, 16)
  };
}"""
DEMOQA_FONT_STYLE_DIAGNOSTIC = r"""({ variant }) => {
  if (location.origin !== 'https://demoqa.com' || window !== window.top) return null;
  const candidates = [
    { token: 'main-header', nodes: document.querySelectorAll('.main-header') },
    { token: 'main-h1', nodes: document.querySelectorAll('main h1') },
    { token: 'h1', nodes: document.querySelectorAll('h1') }
  ];
  const choice = candidates.find(candidate => candidate.nodes.length === 1);
  const heading = choice ? choice.nodes[0] : null;
  if (!heading) return null;
  const variants = {
    baseline: null,
    arial_normal: { family: 'Arial, sans-serif', weight: '400' },
    arial_medium: { family: 'Arial, sans-serif', weight: '500' },
    arial_thin100: { family: 'Arial, sans-serif', weight: '100' },
    system_normal: { family: 'system-ui, sans-serif', weight: '400' },
    system_bold: { family: 'system-ui, sans-serif', weight: '700' },
    system_thin100: { family: 'system-ui, sans-serif', weight: '100' },
    baseline_normal: { family: null, weight: '400' },
    monospace: { family: 'monospace', weight: '400' }
  };
  if (!Object.prototype.hasOwnProperty.call(variants, variant)) return null;
  const style = heading.style;
  const saved = heading.dataset.codexFontStyleOriginal;
  if (variant === 'baseline') {
    if (saved) {
      try {
        const original = JSON.parse(saved);
        if (!Array.isArray(original) || original.length !== 2
            || original.some((item, index) => !Array.isArray(item) || item.length !== 3
              || item[0] !== ['font-family', 'font-weight'][index]
              || typeof item[1] !== 'string' || item[1].length > 128
              || !['', 'important'].includes(item[2]))) return null;
        for (const [property, value, priority] of original) {
          if (value) style.setProperty(property, value, priority);
          else style.removeProperty(property);
        }
      } catch (_) { return null; }
      delete heading.dataset.codexFontStyleOriginal;
    }
  } else {
    if (!saved) {
      const originalFamily = style.getPropertyValue('font-family');
      const originalWeight = style.getPropertyValue('font-weight');
      const originalFamilyPriority = style.getPropertyPriority('font-family');
      const originalWeightPriority = style.getPropertyPriority('font-weight');
      if (originalFamily.length > 128 || originalWeight.length > 128
          || !['', 'important'].includes(originalFamilyPriority)
          || !['', 'important'].includes(originalWeightPriority)) return null;
      heading.dataset.codexFontStyleOriginal = JSON.stringify([
        ['font-family', originalFamily, originalFamilyPriority],
        ['font-weight', originalWeight, originalWeightPriority]
      ]);
    }
    if (variants[variant].family === null) {
      const original = JSON.parse(heading.dataset.codexFontStyleOriginal);
      const originalFamily = original[0][1];
      if (originalFamily) style.setProperty('font-family', originalFamily, original[0][2]);
      else style.removeProperty('font-family');
    } else {
      style.setProperty('font-family', variants[variant].family);
    }
    style.setProperty('font-weight', variants[variant].weight);
  }
  const computed = getComputedStyle(heading);
  return {
    variant,
    heading_selector: choice.token,
    heading_text: (heading.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 64),
    font_family: computed.fontFamily.slice(0, 128),
    font_weight: computed.fontWeight.slice(0, 16)
  };
}"""
_KEYS = frozenset({
    "Escape", "Tab", "Backspace", "Delete", "Home", "End", "PageUp", "PageDown",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Shift", "Control", "Alt",
    "Enter", "Space", "a", "c", "v", "x", "z", "A", "C", "V", "X", "Z",
})
_MODIFIERS = frozenset({"Control", "Shift", "Alt"})
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)


class WorkerError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if not host or any(not _DOMAIN_LABEL.fullmatch(label) for label in host.rstrip(".").split(".")):
            # IPv6 and IPv4 literals are handled below.
            address = ipaddress.ip_address(host.strip("[]"))
            host = f"[{address.compressed}]" if address.version == 6 else str(address)
        else:
            host = host.rstrip(".")
        suffix = f":{port}" if port and port != 443 else ""
        return f"https://{host}{suffix}"
    except (ValueError, UnicodeError) as exc:
        raise WorkerError("INVALID_ORIGIN", "URL must use HTTPS and contain a valid origin without credentials", status_code=422) from exc


def _parse_allowlist(values: list[str]) -> frozenset[str]:
    origins: set[str] = set()
    for value in values:
        origin = _origin(value)
        parsed = urlsplit(value)
        if parsed.path != "" or parsed.query or parsed.fragment:
            raise WorkerError("INVALID_ORIGIN", "allowed_origins must contain origins only", status_code=422)
        origins.add(origin)
    return frozenset(origins)


def _safe_url(url: Any) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 4096:
        raise WorkerError("INVALID_URL", "URL must be a string of at most 4096 characters", status_code=422)
    origin = _origin(url)
    return url, origin


def _auth_path_family(origin: str, path: str) -> str:
    """Return only a fixed diagnostic label; never retain an auth URL path."""
    if origin not in GEMINI_AUTH_DIAGNOSTIC_ORIGINS or not isinstance(path, str):
        return "other"
    if origin == "https://accounts.google.com":
        exact = _AUTH_PATH_FAMILIES.get(path)
        if exact:
            return exact
    if origin == "https://ogs.google.com":
        match = re.fullmatch(r"/u/[0-9]{1,2}/widget/(app|account)", path)
        if match:
            return f"ogs_widget_{match.group(1)}"
    return "other"


def _resource_method_family(method: Any) -> str:
    return method if isinstance(method, str) and method in _RESOURCE_METHOD_FAMILIES else "OTHER"


async def _public_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
        return address.is_global
    except ValueError:
        if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".local", ".internal", ".localhost")):
            return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    return bool(addresses) and all(address.is_global for address in addresses)


def _adapter_class() -> type:
    if not ADAPTER_PATH.is_file():
        raise RuntimeError("vendored PlaywrightBrowserAdapter source is missing")
    lock_path = ROOT / "config" / "artifacts.lock.json"
    try:
        pinned_hash = json.loads(lock_path.read_text(encoding="utf-8"))["browser_fork"]["adapter_sha256"]
        actual_hash = hashlib.sha256(ADAPTER_PATH.read_bytes()).hexdigest()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("vendored adapter pin is missing or invalid") from exc
    if not isinstance(pinned_hash, str) or not secrets.compare_digest(actual_hash, pinned_hash):
        raise RuntimeError("vendored PlaywrightBrowserAdapter does not match the pinned SHA256")
    spec = importlib.util.spec_from_file_location("browser_mcp_vendored_adapter", ADAPTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load vendored PlaywrightBrowserAdapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PlaywrightBrowserAdapter


class GateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: Literal["agent", "human", "none"]


class EpochBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control_epoch: int = Field(ge=0, strict=True)


class SiteCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["gemini_prompt_submit"]
    grant_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class AgentExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=128)
    control_epoch: int = Field(ge=0)
    observation_id: str = Field(min_length=1, max_length=128)
    action: dict[str, Any]
    allowed_origins: list[str] = Field(default_factory=list, max_length=64)
    allow_write: bool = False
    site_capability: SiteCapability | None = None


class ManualExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    control_epoch: int = Field(ge=0)
    action: dict[str, Any]
    allowed_origins: list[str] | None = Field(default=None, max_length=64)


class CookieImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=128)
    cookies: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class RecoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Receipt:
    outcome: str
    action_id: str
    result: dict[str, Any] | None = None


class BrowserRuntime:
    """Owns one persistent browser context and fences every input operation."""

    def __init__(
        self, data_dir: str | Path, backend: str = "camoufox", headless: bool = False,
        egress_ready: bool = False, *, adapter: Any = None, page: Any = None, context: Any = None,
        test_mode: bool = False, proxy_assignment_id: str = "unassigned", route_generation: int = 0,
    ) -> None:
        if backend not in {"camoufox", "chromium"}:
            raise ValueError("backend must be camoufox or chromium")
        if backend == "chromium" and not test_mode:
            raise ValueError("Chromium backend is restricted to explicit test/debug mode")
        self.data_dir = Path(data_dir).resolve()
        self.backend = backend
        self.runtime_environment = "docker" if Path("/.dockerenv").is_file() else "host"
        self.headless = headless
        self.egress_ready = bool(egress_ready)
        self.test_mode = test_mode
        self.worker_instance_id = f"worker-{uuid.uuid4()}"
        self.browser_instance_id = f"browser-instance-{uuid.uuid4()}"
        self.session_id = str(uuid.uuid4())
        self.browser_id = "browser-001"
        self.proxy_assignment_id = proxy_assignment_id
        if isinstance(route_generation, bool) or not isinstance(route_generation, int) or route_generation < 0:
            raise ValueError("route_generation must be a nonnegative integer")
        self.route_generation = route_generation
        self.page_id = "main"
        self.viewport_width = VIEWPORT["width"]
        self.viewport_height = VIEWPORT["height"]
        self.control_epoch = 0
        self.control_owner: Literal["agent", "human", "none"] = "none"
        self._lease_deadline = 0.0
        self._adapter = adapter
        self._page = page
        self._context = context
        self._started = adapter is not None and page is not None
        self._start_detail: str | None = None
        self._last_observation_id: str | None = None
        self._action_in_progress = False
        self._import_in_progress = False
        self._route_origins: frozenset[str] = frozenset()
        self._blocked_resources: dict[tuple[str, str, str, str, str, str], int] = {}
        self._input_lock = asyncio.Lock()
        self._pointer_position: tuple[float, float] | None = None
        self._start_lock = asyncio.Lock()
        self._capture_lock = asyncio.Lock()
        self._readiness_trace = ReadinessActionTrace()
        self._observation_pending = False
        self._page_event_handler: Any = None
        self._context_page_handler: Any = None
        self._context_close_handler: Any = None
        self._page_crash_handler: Any = None
        self._browser: Any = None
        self._browser_disconnect_handler: Any = None
        self._browser_process_pid: int | None = None
        self._browser_process_tree_pids: set[int] = set()
        self._extra_page_close_tasks: dict[int, asyncio.Task[Any]] = {}
        self._quarantine_task: asyncio.Task[Any] | None = None
        self._recovery_cleanup_task: asyncio.Task[Any] | None = None
        self._closed_extra_pages = 0
        self._main_page_closed = False
        self._main_page_failure_reason = "canonical_page_closed"
        self._resource_violation = False
        self._recovery_needed = False
        self._recovery_state: Literal["idle", "recovering", "backoff", "exhausted", "blocked"] = "idle"
        self._recovery_reason = "healthy"
        self._recovery_teardown_reason = "teardown_unconfirmed"
        self._recovery_teardown_error_type: str | None = None
        self._recovery_generation = 0
        self._recovery_attempt_times: list[float] = []
        self._recovery_retry_after = 0.0
        self._camoufox_cm: Any = None
        self._playwright_manager: Any = None
        self._db_path = self.data_dir / "worker.sqlite3"
        self._db: sqlite3.Connection | None = None
        if self._started:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._open_ledger()
            self._install_page_guards()

    @classmethod
    def from_env(cls) -> "BrowserRuntime":
        env = os.environ
        backend = "camoufox"
        test_mode = env.get("APP_ENV", "production").lower() in {"test", "debug"}
        def env_bool(name: str, default: bool = False) -> bool:
            value = env.get(name)
            if value is None:
                return default
            if value.lower() in {"1", "true", "yes", "on"}:
                return True
            if value.lower() in {"0", "false", "no", "off"}:
                return False
            raise RuntimeError(f"{name} must be a boolean")

        if env.get("BROWSER_TEST_CHROMIUM") == "1":
            if not test_mode:
                raise RuntimeError("BROWSER_TEST_CHROMIUM=1 is permitted only when APP_ENV=test or debug")
            backend = "chromium"
        return cls(
            env.get("WORKER_DATA_DIR", str(ROOT / "worker-state")), backend=backend,
            headless=env_bool("BROWSER_HEADLESS"),
            egress_ready=env_bool("EGRESS_READY"),
            proxy_assignment_id=env.get("PROXY_ASSIGNMENT_ID", "unassigned"),
            route_generation=int(env.get("ROUTE_GENERATION", "0")),
            test_mode=test_mode,
        )

    def _open_ledger(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._db_path, timeout=5, isolation_level=None, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS action_ledger(
                action_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                receipt_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cookie_imports(
                job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
                result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capability_uses(
                grant_id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        self._db = db

    async def start(self) -> None:
        async with self._start_lock:
            await self._start_locked()

    async def _start_locked(self, *, recovery: bool = False) -> None:
        if self._started:
            return
        if self._resource_violation or (self._main_page_closed and not recovery):
            raise RuntimeError("browser recovery is required")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        profile = self.data_dir / "profiles" / "default"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            if self.backend == "camoufox":
                from camoufox.async_api import AsyncCamoufox

                from worker.fingerprint import profile_launch_options

                options = await asyncio.to_thread(
                    profile_launch_options, profile, headless=self.headless,
                    viewport=VIEWPORT, env=self._clean_browser_env(),
                )
                self._camoufox_cm = AsyncCamoufox(persistent_context=True, from_options=options)
                context = await self._camoufox_cm.__aenter__()
            else:
                from playwright.async_api import async_playwright

                self._playwright_manager = await async_playwright().start()
                context = await self._playwright_manager.chromium.launch_persistent_context(
                    str(profile), headless=self.headless, viewport=VIEWPORT,
                    env=self._clean_browser_env(),
                )
            pages = list(context.pages)
            page = pages[0] if pages else await context.new_page()
            self._context, self._page = context, page
            self._main_page_closed = False
            self._main_page_failure_reason = "canonical_page_closed"
            self._install_page_guards()
            for extra in pages[1:]:
                self._schedule_extra_page_close(extra)
            # Await restored-page cleanup before considering startup complete.
            if self._extra_page_close_tasks:
                await asyncio.gather(*tuple(self._extra_page_close_tasks.values()), return_exceptions=True)
            if self._resource_violation:
                raise RuntimeError("browser startup exceeded page cleanup limits")
            if self._resource_status()["context_count"] != 1 or self._resource_status()["page_count"] != 1:
                raise RuntimeError("browser startup did not produce exactly one context and page")
            page.set_default_timeout(5000)
            self._adapter = _adapter_class()(page)
            if hasattr(context, "route"):
                await context.route("**/*", self._route_guard)
            if self.backend == "camoufox":
                self._browser_process_pid = self._find_camoufox_profile_pid(profile)
                self._browser_process_tree_pids = self._process_tree(self._browser_process_pid) if self._browser_process_pid else set()
            if self._db is None:
                self._open_ledger()
            self._started = True
            self._start_detail = None
        except BaseException as exc:
            self._started = False
            self._start_detail = f"browser startup failed ({type(exc).__name__})"
            if recovery:
                self._recovery_needed = True
                self._main_page_closed = True
                self._recovery_reason = "launch_failed"
            if recovery:
                # Recovery callers must confirm teardown before another profile launch.
                if not await self._close_browser_for_recovery_cancellation_safe():
                    self._recovery_state = "blocked"
                    self._recovery_reason = "teardown_unconfirmed"
                    self._recovery_needed = True
            else:
                await self._close_browser()
            raise

    @staticmethod
    def _listen(obj: Any, event: str, callback: Any) -> bool:
        on = getattr(obj, "on", None)
        if callable(on):
            on(event, callback)
            return True
        return False

    def _install_page_guards(self) -> None:
        context, page = self._context, self._page
        if context is None or page is None:
            return
        self._context_page_handler = self._on_new_page
        self._context_close_handler = self._on_context_closed
        self._page_event_handler = self._on_main_page_closed
        self._page_crash_handler = self._on_main_page_crashed
        self._listen(context, "page", self._context_page_handler)
        self._listen(context, "close", self._context_close_handler)
        self._listen(page, "close", self._page_event_handler)
        self._listen(page, "crash", self._page_crash_handler)
        browser = getattr(context, "browser", None)
        if browser is not None:
            self._browser = browser
            self._browser_disconnect_handler = self._on_browser_disconnected
            self._listen(browser, "disconnected", self._browser_disconnect_handler)

    def _mark_main_page_unavailable(self, reason: str) -> None:
        if self._main_page_closed:
            return
        self._main_page_closed = True
        self._main_page_failure_reason = reason
        self._recovery_needed = True
        self._recovery_reason = reason
        if not self._resource_violation:
            self._recovery_state = "backoff"
            delay_index = min(len(self._recovery_attempt_times), len(RECOVERY_BACKOFF_SECONDS) - 1)
            self._recovery_retry_after = time.monotonic() + RECOVERY_BACKOFF_SECONDS[delay_index]
        self._started = False
        self.control_epoch += 1
        self.control_owner = "none"
        self._lease_deadline = 0
        self._last_observation_id = None
        self._start_detail = ("canonical browser page crashed; recovery required" if reason == "browser_disconnected"
                              else "canonical browser page closed; recovery required")

    def _on_main_page_closed(self, *_: Any) -> None:
        self._mark_main_page_unavailable("canonical_page_closed")

    def _on_main_page_crashed(self, *_: Any) -> None:
        self._mark_main_page_unavailable("browser_disconnected")

    def _on_context_closed(self, *_: Any) -> None:
        self._mark_main_page_unavailable("browser_disconnected")

    def _on_browser_disconnected(self, *_: Any) -> None:
        self._mark_main_page_unavailable("browser_disconnected")

    def _page_is_unavailable(self) -> bool:
        if self._main_page_closed:
            return True
        page = self._page
        if page is None:
            return False
        is_closed = getattr(page, "is_closed", None)
        try:
            if callable(is_closed) and is_closed():
                return True
            if getattr(page, "closed", False) is True:
                return True
        except Exception:
            return True
        context = self._context
        if context is not None:
            try:
                pages = list(context.pages)
                if page not in pages:
                    return True
            except Exception:
                return True
        try:
            browser = self._browser or (getattr(context, "browser", None) if context is not None else None)
        except Exception:
            return True
        connected = getattr(browser, "is_connected", None)
        try:
            if callable(connected) and not connected():
                return True
        except Exception:
            return True
        return False

    def _on_new_page(self, page: Any) -> None:
        if page is self._page:
            return
        self._schedule_extra_page_close(page)

    def _schedule_extra_page_close(self, page: Any) -> None:
        if page is self._page:
            return
        identity = id(page)
        if identity in self._extra_page_close_tasks:
            return
        if len(self._extra_page_close_tasks) >= MAX_PENDING_PAGE_CLOSES:
            self._trip_resource_violation()
            return
        try:
            task = asyncio.get_running_loop().create_task(self._close_extra_page(page))
        except RuntimeError:
            return
        self._extra_page_close_tasks[identity] = task
        task.add_done_callback(lambda completed, key=identity: self._extra_page_close_tasks.pop(key, None))

    async def _close_extra_page(self, page: Any) -> None:
        try:
            await asyncio.wait_for(page.close(), timeout=PAGE_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            self._trip_resource_violation()
            return
        self._closed_extra_pages += 1

    def _trip_resource_violation(self, detail: str = "browser page cleanup exceeded resource limits; restart required") -> None:
        if self._resource_violation:
            return
        self._resource_violation = True
        self._recovery_needed = False
        self._recovery_state = "blocked"
        self._recovery_reason = "resource_violation"
        self._started = False
        self.control_epoch += 1
        self.control_owner = "none"
        self._lease_deadline = 0
        self._last_observation_id = None
        self._start_detail = detail
        try:
            self._quarantine_task = asyncio.get_running_loop().create_task(self._quarantine_browser())
        except RuntimeError:
            pass

    async def _quarantine_browser(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in self._extra_page_close_tasks.values() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=0.2)
        self._extra_page_close_tasks.clear()
        context = self._context
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                pass
        if self._camoufox_cm is not None:
            cm, self._camoufox_cm = self._camoufox_cm, None
            try:
                await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                pass
        if self._playwright_manager is not None:
            manager, self._playwright_manager = self._playwright_manager, None
            try:
                await asyncio.wait_for(manager.stop(), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                pass
        self._context = None
        self._page = None
        self._adapter = None

    @staticmethod
    def _clean_browser_env() -> dict[str, str]:
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    @staticmethod
    def _find_camoufox_profile_pid(profile: Path) -> int | None:
        proc = Path("/proc")
        if not proc.is_dir():
            return None
        profile_bytes = str(profile).encode("utf-8")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if (entry / "comm").read_text(encoding="utf-8").strip() != "camoufox-bin":
                    continue
                argv = (entry / "cmdline").read_bytes().split(b"\0")
                if b"-contentproc" in argv or b"-profile" not in argv:
                    continue
                profile_index = argv.index(b"-profile") + 1
                if profile_index < len(argv) and argv[profile_index] == profile_bytes:
                    return int(entry.name)
            except (FileNotFoundError, PermissionError, ValueError):
                continue
        return None

    @staticmethod
    def _process_tree(root_pid: int) -> set[int]:
        proc = Path("/proc")
        if not proc.is_dir():
            return set()
        parent_by_pid: dict[int, int] = {}
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
                rest = stat[stat.rfind(")") + 2:].split()
                parent_by_pid[int(entry.name)] = int(rest[1])
            except (FileNotFoundError, PermissionError, ValueError):
                continue
        tree = {root_pid}
        while True:
            expanded = tree | {pid for pid, parent in parent_by_pid.items() if parent in tree}
            if expanded == tree:
                return tree
            tree = expanded

    @staticmethod
    def _process_is_live(pid: int) -> bool:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            return stat[stat.rfind(")") + 2:].split()[0] != "Z"
        except (FileNotFoundError, PermissionError, ValueError):
            return False

    async def _wait_for_browser_process_tree_exit(self, pids: set[int]) -> bool:
        if not pids:
            return True
        deadline = time.monotonic() + RECOVERY_PROCESS_EXIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not any(self._process_is_live(pid) for pid in pids):
                return True
            await asyncio.sleep(0.05)
        return not any(self._process_is_live(pid) for pid in pids)

    def _refresh_browser_process_tree(self) -> None:
        if self._browser_process_pid is not None and self._process_is_live(self._browser_process_pid):
            self._browser_process_tree_pids.update(self._process_tree(self._browser_process_pid))

    async def _close_browser(self) -> None:
        quarantine = self._quarantine_task
        if quarantine is not None and quarantine is not asyncio.current_task():
            await asyncio.wait({quarantine}, timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS * 3 + 0.2)
            if not quarantine.done():
                quarantine.cancel()
                await asyncio.wait({quarantine}, timeout=0.2)
            self._quarantine_task = None
        context, page = self._context, self._page
        if context is not None and self._context_page_handler is not None:
            self._remove_listener(context, "page", self._context_page_handler)
        if context is not None and self._context_close_handler is not None:
            self._remove_listener(context, "close", self._context_close_handler)
        if page is not None and self._page_event_handler is not None:
            self._remove_listener(page, "close", self._page_event_handler)
        if page is not None and self._page_crash_handler is not None:
            self._remove_listener(page, "crash", self._page_crash_handler)
        if self._browser is not None and self._browser_disconnect_handler is not None:
            self._remove_listener(self._browser, "disconnected", self._browser_disconnect_handler)
        self._context_page_handler = self._page_event_handler = None
        self._context_close_handler = self._page_crash_handler = None
        self._browser_disconnect_handler = None
        if self._extra_page_close_tasks:
            pending = tuple(self._extra_page_close_tasks.values())
            _, pending = await asyncio.wait(pending, timeout=PAGE_CLOSE_TIMEOUT_SECONDS + 0.1)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=0.2)
            self._extra_page_close_tasks.clear()
        if self._camoufox_cm is not None:
            cm, self._camoufox_cm = self._camoufox_cm, None
            try:
                await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                pass
        elif self._context is not None:
            try:
                await asyncio.wait_for(self._context.close(), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                pass
        self._context = None
        self._page = None
        self._adapter = None
        if self._playwright_manager is not None:
            manager, self._playwright_manager = self._playwright_manager, None
            try:
                await asyncio.wait_for(manager.stop(), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                pass
        self._browser = None

    async def _close_browser_for_recovery(self) -> bool:
        """Close the old persistent context and confirm its owner exited before relaunch."""
        self._refresh_browser_process_tree()
        quarantine = self._quarantine_task
        if quarantine is not None and quarantine is not asyncio.current_task():
            done, _ = await asyncio.wait({quarantine}, timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS * 3 + 0.2)
            if not done:
                return False
            self._quarantine_task = None

        context, page = self._context, self._page
        if context is not None and self._context_page_handler is not None:
            self._remove_listener(context, "page", self._context_page_handler)
        if context is not None and self._context_close_handler is not None:
            self._remove_listener(context, "close", self._context_close_handler)
        if page is not None and self._page_event_handler is not None:
            self._remove_listener(page, "close", self._page_event_handler)
        if page is not None and self._page_crash_handler is not None:
            self._remove_listener(page, "crash", self._page_crash_handler)
        if self._browser is not None and self._browser_disconnect_handler is not None:
            self._remove_listener(self._browser, "disconnected", self._browser_disconnect_handler)
        self._context_page_handler = self._context_close_handler = None
        self._page_event_handler = self._page_crash_handler = None
        self._browser_disconnect_handler = None

        if self._extra_page_close_tasks:
            pending = tuple(self._extra_page_close_tasks.values())
            _, pending = await asyncio.wait(pending, timeout=PAGE_CLOSE_TIMEOUT_SECONDS + 0.1)
            if pending:
                self._recovery_teardown_reason = "pending_page_close"
                return False
            self._extra_page_close_tasks.clear()

        old_process_tree = set(self._browser_process_tree_pids)
        try:
            if self._camoufox_cm is not None:
                await asyncio.wait_for(self._camoufox_cm.__aexit__(None, None, None),
                                       timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
                self._camoufox_cm = None
            elif self._context is not None:
                await asyncio.wait_for(self._context.close(), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            self._recovery_teardown_reason = "context_close_timeout"
            self._recovery_teardown_error_type = type(exc).__name__
            return False
        except Exception as exc:
            try:
                from playwright._impl._errors import TargetClosedError
            except ImportError:
                TargetClosedError = ()
            if not (TargetClosedError and isinstance(exc, TargetClosedError) and self._camoufox_cm is not None):
                self._recovery_teardown_reason = "context_close_failed"
                self._recovery_teardown_error_type = type(exc).__name__
                return False
            try:
                from camoufox.async_api import PlaywrightContextManager

                # AsyncCamoufox.__aexit__ skips this base cleanup when browser.close()
                # reports TargetClosedError, leaving its Playwright driver alive.
                await asyncio.wait_for(PlaywrightContextManager.__aexit__(self._camoufox_cm, None, None),
                                       timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
                self._camoufox_cm = None
            except asyncio.TimeoutError as stop_exc:
                self._recovery_teardown_reason = "driver_stop_timeout"
                self._recovery_teardown_error_type = type(stop_exc).__name__
                return False
            except Exception as stop_exc:
                self._recovery_teardown_reason = "driver_stop_failed"
                self._recovery_teardown_error_type = type(stop_exc).__name__
                return False

        try:
            # The close manager must be fully stopped before reusing the profile.
            if self._playwright_manager is not None:
                await asyncio.wait_for(self._playwright_manager.stop(), timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS)
                self._playwright_manager = None
        except asyncio.TimeoutError:
            self._recovery_teardown_reason = "driver_stop_timeout"
            return False
        except Exception:
            self._recovery_teardown_reason = "driver_stop_failed"
            return False

        if not await self._wait_for_browser_process_tree_exit(old_process_tree):
            self._recovery_teardown_reason = "browser_process_alive"
            return False

        self._context = None
        self._page = None
        self._adapter = None
        self._browser = None
        self._browser_process_pid = None
        self._browser_process_tree_pids = set()
        return True

    async def _close_browser_for_recovery_cancellation_safe(self) -> bool:
        cleanup = asyncio.create_task(self._close_browser_for_recovery())
        self._recovery_cleanup_task = cleanup
        try:
            result = await asyncio.shield(cleanup)
            if self._recovery_cleanup_task is cleanup:
                self._recovery_cleanup_task = None
            return result
        except asyncio.CancelledError:
            confirmed = False
            try:
                confirmed = await asyncio.wait_for(
                    asyncio.shield(cleanup),
                    timeout=CONTEXT_SHUTDOWN_TIMEOUT_SECONDS * 3 + PAGE_CLOSE_TIMEOUT_SECONDS + 0.5,
                )
            except Exception:
                pass
            if cleanup.done() and self._recovery_cleanup_task is cleanup:
                self._recovery_cleanup_task = None
            if not confirmed:
                self._recovery_state = "blocked"
                self._recovery_needed = True
                self._recovery_reason = self._recovery_teardown_reason
            raise

    @staticmethod
    def _remove_listener(obj: Any, event: str, callback: Any) -> None:
        remove = getattr(obj, "remove_listener", None) or getattr(obj, "off", None)
        if callable(remove):
            try:
                remove(event, callback)
            except Exception:
                pass

    async def close(self) -> None:
        self.control_epoch += 1
        self.control_owner = "none"
        self._lease_deadline = 0
        self._blocked_resources.clear()
        await self._close_browser()
        if self._db is not None:
            self._db.close()
            self._db = None
        self._started = False

    async def health(self) -> dict[str, Any]:
        configured = self._adapter is not None or self.backend in {"camoufox", "chromium"}
        route_ready = self.egress_ready and self.route_generation > 0
        resources = self._resource_status()
        page_ready = (self._started and not self._page_is_unavailable() and not self._resource_violation
                      and self._page is not None and resources["context_count"] == 1
                      and resources["page_count"] == 1)
        ready = page_ready and route_ready
        detail = ("browser ready; explicit direct development route selected" if self.proxy_assignment_id == "direct-development"
                  else "browser and assigned egress route are ready") if ready else (
            self._start_detail or (("external egress route is not verified; live navigation is disabled"
                                    if not self.egress_ready or self.route_generation == 0 else "browser is not started")
                                   if self._started else "browser is not started")
        )
        return {"configured": configured, "ready": ready, "browser_started": self._started, "detail": detail,
                "browser_instance_id": self.browser_instance_id,
                "runtime_environment": self.runtime_environment,
                "backend": self.backend, "egress_ready": self.egress_ready,
                "proxy_assignment_id": self.proxy_assignment_id, "route_generation": self.route_generation,
                **resources, **self._recovery_status(),
                "blocked_resources": [{"origin": origin, "resource_type": resource_type, "reason": reason,
                                        "path_family": path_family, "frame": frame, "method": method, "count": count}
                                      for (origin, resource_type, reason, path_family, frame, method), count in self._blocked_resources.items()]}

    def _resource_status(self) -> dict[str, Any]:
        context_count = int(self._context is not None)
        try:
            page_count = len(self._context.pages) if self._context is not None else 0
        except Exception:
            page_count = int(self._page is not None and not self._main_page_closed)
        return {"context_count": context_count, "page_count": page_count, "page_limit": 1,
                "closed_extra_pages": self._closed_extra_pages,
                "pending_page_closes": len(self._extra_page_close_tasks),
                "resource_violation": self._resource_violation}

    def _recovery_status(self) -> dict[str, Any]:
        now = time.monotonic()
        self._recovery_attempt_times = [stamp for stamp in self._recovery_attempt_times
                                        if now - stamp < RECOVERY_WINDOW_SECONDS]
        needed = self._recovery_needed or self._page_is_unavailable()
        state = self._recovery_state
        reason = self._recovery_reason
        if self._resource_violation:
            state, reason = "blocked", "resource_violation"
        elif not self.egress_ready or self.route_generation <= 0:
            if needed:
                state, reason = "blocked", "egress_unavailable"
        elif not needed and self._started:
            state, reason = "idle", "healthy"
        elif needed and state == "idle" and reason == "healthy":
            reason = self._main_page_failure_reason
        if state == "exhausted" and len(self._recovery_attempt_times) < RECOVERY_MAX_ATTEMPTS:
            state = "backoff" if now < self._recovery_retry_after else "idle"
            reason = "backoff" if state == "backoff" else self._main_page_failure_reason
        retry_after = max(0.0, min(30.0, self._recovery_retry_after - now)) if self._recovery_retry_after else None
        return {"recovery_needed": bool(needed), "recovery_state": state,
                "recovery_reason": reason, "recovery_generation": self._recovery_generation,
                "recovery_attempts": len(self._recovery_attempt_times),
                "recovery_retry_after_seconds": round(retry_after, 3) if retry_after is not None else None}

    def status_base(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "worker_instance_id": self.worker_instance_id,
                "browser_instance_id": self.browser_instance_id,
                "backend": self.backend, "runtime_environment": self.runtime_environment,
                "browser_id": self.browser_id, "page_id": self.page_id,
                "browser_started": self._started, "width": self.viewport_width, "height": self.viewport_height,
                "proxy_assignment_id": self.proxy_assignment_id, "route_generation": self.route_generation,
                "control_epoch": self.control_epoch, "control_owner": self.control_owner,
                **self._resource_status(),
                **self._recovery_status(),
                "blocked_resources": [{"origin": origin, "resource_type": resource_type, "reason": reason,
                                        "path_family": path_family, "frame": frame, "method": method, "count": count}
                                      for (origin, resource_type, reason, path_family, frame, method), count in self._blocked_resources.items()]}

    async def status(self) -> dict[str, Any]:
        url = None
        if self._adapter is not None:
            try:
                url = await self._adapter.url()
            except Exception:
                url = None
        return {**self.status_base(), "url": url, "action_in_progress": self._action_in_progress,
                "import_in_progress": self._import_in_progress}

    def _recovery_is_busy(self) -> bool:
        return bool(self._action_in_progress or self._import_in_progress or self._input_lock.locked()
                    or self._observation_pending or self._capture_lock.locked()
                    or any(not task.done() for task in self._extra_page_close_tasks.values()))

    def _recovery_result(self, outcome: str, reason: str) -> dict[str, Any]:
        self._recovery_reason = reason
        status = self._recovery_status()
        return {"outcome": outcome, **status, "browser_started": self._started,
                **self._resource_status()}

    async def recover(self, *, automatic: bool = False) -> dict[str, Any]:
        async with self._start_lock:
            if self._page_is_unavailable() and not self._main_page_closed:
                self._mark_main_page_unavailable(self._main_page_failure_reason)
            if self._started and not self._page_is_unavailable() and not self._resource_violation:
                return self._recovery_result("already_ready", "healthy")
            if self._resource_violation:
                self._recovery_state = "blocked"
                return self._recovery_result("blocked", "resource_violation")
            if not self.egress_ready or self.route_generation <= 0:
                self._recovery_state = "blocked"
                return self._recovery_result("blocked", "egress_unavailable")
            if self._recovery_state == "blocked":
                return self._recovery_result("blocked", self._recovery_reason)
            if not self._recovery_needed:
                self._recovery_state = "blocked"
                return self._recovery_result("blocked", "not_recoverable")
            if self._recovery_is_busy():
                return self._recovery_result("deferred", "operation_in_progress")
            if self._quarantine_task is not None and not self._quarantine_task.done():
                return self._recovery_result("deferred", "operation_in_progress")
            if self._recovery_cleanup_task is not None and not self._recovery_cleanup_task.done():
                return self._recovery_result("deferred", "operation_in_progress")
            if self._recovery_cleanup_task is not None:
                self._recovery_cleanup_task = None

            now = time.monotonic()
            self._recovery_attempt_times = [stamp for stamp in self._recovery_attempt_times
                                            if now - stamp < RECOVERY_WINDOW_SECONDS]
            if len(self._recovery_attempt_times) >= RECOVERY_MAX_ATTEMPTS:
                self._recovery_state = "exhausted"
                return self._recovery_result("blocked", "attempts_exhausted")
            if now < self._recovery_retry_after:
                self._recovery_state = "backoff"
                return self._recovery_result("blocked", "backoff")

            self._recovery_attempt_times.append(now)
            self._recovery_retry_after = 0.0
            self._recovery_state = "recovering"
            self._recovery_reason = self._main_page_failure_reason
            if not self._main_page_closed:
                self._mark_main_page_unavailable(self._main_page_failure_reason)
                self._recovery_state = "recovering"
            self._started = False
            self.control_epoch += 1
            self.control_owner = "none"
            self._lease_deadline = 0
            self._last_observation_id = None
            self._route_origins = frozenset()
            try:
                confirmed = await self._close_browser_for_recovery_cancellation_safe()
            except asyncio.CancelledError:
                if self._recovery_state != "blocked":
                    self._recovery_state = "backoff"
                    self._recovery_reason = "launch_failed"
                    self._recovery_retry_after = time.monotonic() + RECOVERY_BACKOFF_SECONDS[0]
                raise
            if not confirmed:
                self._recovery_state = "blocked"
                self._recovery_needed = True
                return self._recovery_result("failed", self._recovery_teardown_reason)
            if self._db is not None:
                try:
                    self._db.close()
                except sqlite3.Error:
                    self._recovery_state = "blocked"
                    return self._recovery_result("failed", "teardown_unconfirmed")
                self._db = None
            try:
                await asyncio.wait_for(self._start_locked(recovery=True), timeout=RECOVERY_START_TIMEOUT_SECONDS)
                if (not self._started or self._resource_violation or
                        self._resource_status()["context_count"] != 1 or
                        self._resource_status()["page_count"] != 1):
                    raise RuntimeError("recovered browser did not satisfy readiness invariants")
            except asyncio.CancelledError:
                self._started = False
                self._recovery_needed = True
                if self._recovery_state != "blocked":
                    self._recovery_state = "backoff"
                    self._recovery_reason = "launch_failed"
                    self._recovery_retry_after = time.monotonic() + RECOVERY_BACKOFF_SECONDS[0]
                raise
            except Exception:
                self._started = False
                self._recovery_needed = True
                if self._recovery_state != "blocked":
                    if len(self._recovery_attempt_times) >= RECOVERY_MAX_ATTEMPTS:
                        self._recovery_state = "exhausted"
                        self._recovery_retry_after = now + RECOVERY_WINDOW_SECONDS
                        self._recovery_reason = "attempts_exhausted"
                    else:
                        self._recovery_state = "backoff"
                        delay_index = min(len(self._recovery_attempt_times), len(RECOVERY_BACKOFF_SECONDS) - 1)
                        delay = RECOVERY_BACKOFF_SECONDS[delay_index]
                        self._recovery_retry_after = time.monotonic() + delay
                    self._recovery_reason = "launch_failed"
                return self._recovery_result("failed", self._recovery_reason)

            self._recovery_generation += 1
            self._recovery_needed = False
            self._recovery_state = "idle"
            self._recovery_reason = "healthy"
            self._recovery_retry_after = 0.0
            self._main_page_closed = False
            self._main_page_failure_reason = "canonical_page_closed"
            self._start_detail = None
            return self._recovery_result("recovered", "healthy")

    async def recovery_watchdog(self) -> None:
        while True:
            await asyncio.sleep(RECOVERY_WATCHDOG_INTERVAL_SECONDS)
            self._refresh_browser_process_tree()
            if self._resource_violation or not self.egress_ready or self.route_generation <= 0:
                continue
            if self._page_is_unavailable() and not self._main_page_closed:
                self._mark_main_page_unavailable(self._main_page_failure_reason)
            if self._started and not self._recovery_is_busy() and self._page is not None:
                try:
                    await asyncio.wait_for(self._page.evaluate("() => 1"), timeout=LIVENESS_PROBE_TIMEOUT_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    try:
                        from playwright._impl._errors import TargetClosedError
                    except ImportError:
                        TargetClosedError = ()
                    if TargetClosedError and isinstance(exc, TargetClosedError):
                        self._mark_main_page_unavailable("browser_disconnected")
            if self._recovery_needed:
                try:
                    await self.recover(automatic=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._recovery_state = "backoff"
                    self._recovery_reason = "launch_failed"
                    self._recovery_retry_after = time.monotonic() + RECOVERY_BACKOFF_SECONDS[0]

    async def _read_gemini_auth_status(self) -> dict[str, str]:
        from worker.site_capabilities import GEMINI_ORIGIN, inspect_gemini_auth_status

        if not self._started or self._page is None or self._adapter is None:
            return {"status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}
        try:
            if _origin(await self._adapter.url()) != GEMINI_ORIGIN:
                return {"status": "unknown", "reason_code": "NOT_GEMINI"}
            result = await asyncio.wait_for(inspect_gemini_auth_status(self._page), timeout=2)
            if _origin(await self._adapter.url()) != GEMINI_ORIGIN:
                return {"status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}
            return result
        except Exception:
            return {"status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}

    async def gemini_auth_status(self) -> dict[str, str]:
        if self._action_in_progress or self._import_in_progress or self._input_lock.locked():
            return {"status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}
        async with self._input_lock:
            if self._action_in_progress or self._import_in_progress:
                return {"status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}
            return await self._read_gemini_auth_status()

    async def _require_gemini_auth(self) -> None:
        result = await self._read_gemini_auth_status()
        if result["status"] == "signed_in":
            return
        if result["status"] == "signed_out":
            raise WorkerError("AUTH_REQUIRED", "Sign in to Gemini in the browser before continuing", status_code=409)
        raise WorkerError("AUTH_STATE_UNKNOWN", "Gemini sign-in state could not be confirmed", status_code=409)

    async def _require_auth_for_write(self, *, manual: bool, allow_write: bool, kind: str) -> None:
        if manual or not allow_write or kind not in {"click", "drag", "type_text", "press_key"}:
            return
        from worker.site_capabilities import GEMINI_ORIGIN
        try:
            current_origin = _origin(await self._adapter.url())
        except Exception:
            current_origin = None
        if current_origin == GEMINI_ORIGIN:
            await self._require_gemini_auth()

    def gate(self, owner: Literal["agent", "human", "none"]) -> dict[str, Any]:
        if owner != "none" and (self._main_page_closed or self._resource_violation):
            raise WorkerError("BROWSER_UNAVAILABLE", "browser worker is not ready", status_code=503)
        if owner != "none" and self._import_in_progress:
            raise WorkerError("CONTROL_BUSY", "cookie import is in progress")
        # These assignments happen without an intervening await so old inputs
        # are fenced immediately, even if navigation is currently pending.
        self.control_epoch += 1
        self.control_owner = owner
        self._lease_deadline = time.monotonic() + LEASE_SECONDS if owner == "agent" else 0.0
        self._last_observation_id = None
        return self.status_base()

    def heartbeat(self, control_epoch: int) -> dict[str, Any]:
        self._check_epoch(control_epoch)
        if self.control_owner != "agent":
            raise WorkerError("CONTROL_NOT_AGENT", "agent heartbeat requires agent control")
        self._lease_deadline = time.monotonic() + LEASE_SECONDS
        return {**self.status_base(), "lease_expires_in_s": LEASE_SECONDS}

    def _check_epoch(self, epoch: int) -> None:
        if epoch != self.control_epoch:
            raise WorkerError("STALE_CONTROL_EPOCH", "control_epoch is stale")

    def _check_agent_gate(self, epoch: int) -> None:
        self._check_epoch(epoch)
        if self.control_owner != "agent":
            raise WorkerError("CONTROL_NOT_AGENT", "agent input is not the current owner")
        if time.monotonic() >= self._lease_deadline:
            self.control_owner = "none"
            self.control_epoch += 1
            self._last_observation_id = None
            raise WorkerError("AGENT_LEASE_EXPIRED", "agent control lease expired")
        if not self.egress_ready or self.route_generation == 0:
            raise WorkerError("EGRESS_NOT_READY", "agent execution is disabled until the external egress route is verified", status_code=503)
        if not self._started or self._page is None or self._adapter is None or self._main_page_closed or self._resource_violation:
            raise WorkerError("BROWSER_UNAVAILABLE", "browser worker is not ready", status_code=503)

    async def observe(self) -> dict[str, Any]:
        if not self._started or self._adapter is None or self._main_page_closed or self._resource_violation:
            raise WorkerError("BROWSER_UNAVAILABLE", "browser worker is not ready", status_code=503)
        if self._action_in_progress:
            raise WorkerError("ACTION_IN_PROGRESS", "cannot create an observation while an action is running")
        if self._observation_pending:
            raise WorkerError("CAPTURE_BUSY", "another browser observation is pending")
        self._observation_pending = True
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._capture_lock.acquire(), timeout=OBSERVATION_PRIORITY_WAIT_SECONDS)
                acquired = True
            except TimeoutError as exc:
                raise WorkerError("CAPTURE_BUSY", "viewer capture did not finish in time for observation") from exc
            screenshot = await self._capture_screenshot()
            if len(screenshot) > MAX_SCREENSHOT_BYTES:
                raise WorkerError("SCREENSHOT_TOO_LARGE", "viewport screenshot exceeded 16 MiB", status_code=413)
            try:
                size = await self._page.evaluate("() => ({width: innerWidth, height: innerHeight})")
            except Exception:
                size = VIEWPORT
            width, height = int(size.get("width", VIEWPORT["width"])), int(size.get("height", VIEWPORT["height"]))
            observation_id = str(uuid.uuid4())
            self._last_observation_id = observation_id
            observation = {**self.status_base(), "page_id": self.page_id, "observation_id": observation_id,
                           "width": width, "height": height,
                           "url": await self._adapter.url(), "screenshot_png_b64": base64.b64encode(screenshot).decode("ascii")}
            if observation_hints_enabled():
                observation["interaction_hints"] = await collect_interaction_hints(self._page, width, height)
            return observation
        finally:
            self._observation_pending = False
            if acquired:
                self._capture_lock.release()

    async def screenshot(self) -> bytes:
        if not self._started or self._adapter is None or self._main_page_closed or self._resource_violation:
            raise WorkerError("BROWSER_UNAVAILABLE", "browser worker is not ready", status_code=503)
        if self._observation_pending or self._capture_lock.locked():
            raise WorkerError("CAPTURE_BUSY", "another browser capture is in progress")
        async with self._capture_lock:
            image = await self._capture_screenshot()
            if len(image) > MAX_SCREENSHOT_BYTES:
                raise WorkerError("SCREENSHOT_TOO_LARGE", "viewport screenshot exceeded 16 MiB", status_code=413)
            return image

    async def _capture_screenshot(self) -> bytes:
        try:
            return await asyncio.wait_for(
                self._adapter.screenshot(full_page=False, fmt="png"), timeout=CAPTURE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise WorkerError("CAPTURE_TIMEOUT", "browser screenshot exceeded its time limit", status_code=504) from exc

    async def reset_view(self, epoch: int) -> dict[str, Any]:
        """Clear an out-of-scope old page without clearing the persistent session."""
        self._check_agent_gate(epoch)
        if self._action_in_progress or self._import_in_progress:
            raise WorkerError("CONTROL_BUSY", "another browser operation is in progress")
        self._action_in_progress = True
        self._last_observation_id = None
        self._route_origins = frozenset()
        self._blocked_resources.clear()
        try:
            await self._adapter.goto("about:blank", wait_until="domcontentloaded", timeout_ms=5000)
            self._check_agent_gate(epoch)
            return {"outcome": "applied", "url": "about:blank", **self.status_base()}
        finally:
            self._action_in_progress = False

    async def verify(self, conditions: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._started or self._adapter is None or self._page is None:
            raise WorkerError("BROWSER_UNAVAILABLE", "browser worker is not ready", status_code=503)
        if len(conditions) > 32:
            raise WorkerError("INVALID_CONDITION", "at most 32 verification conditions are allowed", status_code=422)
        checks: list[dict[str, Any]] = []
        for condition in conditions:
            if not isinstance(condition, dict) or not isinstance(condition.get("kind"), str):
                raise WorkerError("INVALID_CONDITION", "each condition needs a kind", status_code=422)
            kind = condition["kind"]
            if kind == "url_is":
                if set(condition) != {"kind", "url"} or not isinstance(condition["url"], str) or len(condition["url"]) > 4096:
                    raise WorkerError("INVALID_CONDITION", "url_is requires only a bounded url", status_code=422)
                passed = await self._adapter.url() == condition["url"]
            elif kind == "text_present":
                if set(condition) != {"kind", "text"} or not isinstance(condition["text"], str) or not condition["text"] or len(condition["text"]) > 2000:
                    raise WorkerError("INVALID_CONDITION", "text_present requires non-empty bounded text", status_code=422)
                try:
                    body_text = await self._page.locator("body").inner_text(timeout=2000)
                    passed = condition["text"] in body_text
                except Exception:
                    passed = False
            elif kind == "field_equals":
                if set(condition) != {"kind", "selector", "value"} or not isinstance(condition["selector"], str) or not condition["selector"] or len(condition["selector"]) > 256 or not isinstance(condition["value"], str) or len(condition["value"]) > 10_000:
                    raise WorkerError("INVALID_CONDITION", "field_equals requires a bounded selector and string value", status_code=422)
                try:
                    actual = await self._page.locator(condition["selector"]).first.input_value(timeout=2000)
                    passed = actual == condition["value"]
                except Exception:
                    passed = False
            elif kind in {"element_checked", "element_enabled", "element_visible", "element_text_contains"}:
                field = {"element_checked": "checked", "element_enabled": "enabled",
                         "element_visible": "visible", "element_text_contains": "text"}[kind]
                selector, expected = condition.get("selector"), condition.get(field)
                if (set(condition) != {"kind", "selector", field} or not isinstance(selector, str)
                        or not selector or len(selector) > 256
                        or (not isinstance(expected, str) or not expected or len(expected) > 10_000
                            if field == "text" else type(expected) is not bool)):
                    raise WorkerError("INVALID_CONDITION", "invalid element verification condition", status_code=422)
                try:
                    locator = self._page.locator(selector)
                    count = await asyncio.wait_for(locator.count(), timeout=2)
                    if count == 0 and kind == "element_visible" and expected is False:
                        passed = True
                    elif count != 1:
                        passed = False
                    elif kind == "element_text_contains":
                        passed = expected in await locator.inner_text(timeout=2000)
                    else:
                        method = {"element_checked": locator.is_checked, "element_enabled": locator.is_enabled,
                                  "element_visible": locator.is_visible}[kind]
                        passed = await asyncio.wait_for(method(timeout=2000), timeout=2) is expected
                except Exception:
                    passed = False
            else:
                raise WorkerError("INVALID_CONDITION", "unknown verification condition kind", status_code=422)
            checks.append({"kind": kind, "passed": bool(passed)})
        return {"verified": bool(checks) and all(check["passed"] for check in checks), "checks": checks}

    async def _route_guard(self, route: Any) -> None:
        origin = "invalid"
        resource_type = "other"
        path_family = "other"
        frame = "unknown"
        method = "OTHER"
        try:
            url, origin = _safe_url(route.request.url)
            host = urlsplit(url).hostname or ""
            path_family = _auth_path_family(origin, urlsplit(url).path)
            frame = self._request_frame_class(route.request)
            method = _resource_method_family(route.request.method)
            kind = route.request.resource_type
            resource_type = kind if kind in {"document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch", "eventsource", "websocket", "manifest", "other"} else "other"
            cdn_resource = ("https://gemini.google.com" in self._route_origins
                            and origin in {"https://gemini.gstatic.com", "https://www.gstatic.com"}
                            and route.request.method in {"GET", "HEAD"}
                            and resource_type != "document" and not route.request.is_navigation_request())
            font_resource = ("https://gemini.google.com" in self._route_origins
                             and origin == "https://fonts.gstatic.com"
                             and route.request.method in {"GET", "HEAD"}
                             and resource_type == "font" and not route.request.is_navigation_request())
            gemini_auth_api_resource = self._is_gemini_auth_api_resource(route, origin, resource_type)
            gemini_auth_document_resource = self._is_gemini_auth_document_resource(route, origin, resource_type, url)
            gemini_auth_frame_resource = self._is_gemini_auth_frame_resource(route, origin, resource_type)
            gemini_static_resource = self._is_gemini_static_resource(route, origin, resource_type)
            if not self.egress_ready or self.route_generation <= 0:
                reason = "egress_unavailable"
            elif (origin not in self._route_origins and not cdn_resource and not font_resource
                  and not gemini_auth_api_resource and not gemini_auth_document_resource
                  and not gemini_auth_frame_resource and not gemini_static_resource):
                reason = "origin_not_allowed"
            elif not await _public_host(host):
                reason = "non_public_or_unresolved_host"
            else:
                await route.continue_()
                return
        except (WorkerError, ValueError, TypeError):
            reason = "invalid_url"
        key = (origin, resource_type, reason, path_family, frame, method)
        if key in self._blocked_resources:
            self._blocked_resources[key] += 1
        elif len(self._blocked_resources) < 32:
            self._blocked_resources[key] = 1
        await route.abort("blockedbyclient")

    def _request_frame_class(self, request: Any) -> str:
        try:
            frame = request.frame
            if frame is None:
                return "unknown"
            page = self._page
            if page is None:
                return "unknown"
            return "main" if frame == page.main_frame else "subframe"
        except Exception:
            return "unknown"

    def _is_gemini_main_frame_context(self, frame: Any) -> bool:
        if "https://gemini.google.com" not in self._route_origins or self._page is None:
            return False
        try:
            if _safe_url(self._page.url)[1] != "https://gemini.google.com":
                return False
            main = self._page.main_frame
            if main is None:
                return False
            current = frame
            seen = set()
            for _ in range(8):
                if current is None:
                    return False
                marker = id(current)
                if marker in seen:
                    return False
                seen.add(marker)
                if current == main:
                    return True
                current = current.parent_frame
            return False
        except Exception:
            return False

    def _is_gemini_auth_api_resource(self, route: Any, origin: str, resource_type: str) -> bool:
        if "https://gemini.google.com" not in self._route_origins or origin not in GEMINI_AUTH_API_ORIGINS:
            return False
        try:
            request = route.request
            if (resource_type not in {"xhr", "fetch", "other"}
                    or request.is_navigation_request()):
                return False
            page = self._page
            if page is None or _safe_url(page.url)[1] != "https://gemini.google.com":
                return False
            main_frame = page.main_frame
            request_frame = request.frame
            if request_frame != main_frame or _safe_url(request_frame.url)[1] != "https://gemini.google.com":
                return False
            headers = getattr(request, "headers", {})
            request_origin = headers.get("origin")
            if request_origin is not None and request_origin != "https://gemini.google.com":
                return False
            if request.method == "OPTIONS":
                requested_method = headers.get("access-control-request-method", "").upper()
                return (request_origin == "https://gemini.google.com"
                        and requested_method in {"GET", "HEAD", "POST"})
            return request.method in {"GET", "HEAD", "POST"}
        except Exception:
            # Requests without an auditable canonical main-frame context fail closed.
            return False

    def _is_gemini_auth_document_resource(self, route: Any, origin: str, resource_type: str, url: str) -> bool:
        if origin not in GEMINI_AUTH_DIAGNOSTIC_ORIGINS:
            return False
        try:
            request = route.request
            if (resource_type != "document" or request.method not in {"GET", "HEAD"}
                    or not request.is_navigation_request()):
                return False
            frame = request.frame
            if self._page is None or frame is None or frame == self._page.main_frame:
                return False
            if not self._is_gemini_main_frame_context(frame):
                return False
            # OGS is needed only for the observed app widget document. Accounts
            # documents remain origin/frame scoped; endpoint paths are diagnostic only.
            return origin == "https://accounts.google.com" or _auth_path_family(origin, urlsplit(url).path) == "ogs_widget_app"
        except Exception:
            return False

    def _is_gemini_auth_frame_resource(self, route: Any, origin: str, resource_type: str) -> bool:
        if origin not in GEMINI_AUTH_DIAGNOSTIC_ORIGINS:
            return False
        try:
            request = route.request
            frame = request.frame
            if (frame is None or request.is_navigation_request()
                    or not self._is_gemini_main_frame_context(frame)
                    or _safe_url(frame.url)[1] != origin):
                return False
            if resource_type in {"xhr", "fetch"}:
                return request.method in {"GET", "HEAD", "POST"}
            return (resource_type in {"script", "stylesheet", "image", "font"}
                    and request.method in {"GET", "HEAD"})
        except Exception:
            return False

    def _is_gemini_static_resource(self, route: Any, origin: str, resource_type: str) -> bool:
        if "https://gemini.google.com" not in self._route_origins or self._page is None:
            return False
        try:
            request = route.request
            if (request.method not in {"GET", "HEAD"} or request.is_navigation_request()
                    or request.frame != self._page.main_frame
                    or _safe_url(self._page.url)[1] != "https://gemini.google.com"
                    or _safe_url(request.frame.url)[1] != "https://gemini.google.com"):
                return False
            return ((origin == "https://www.google.com" and resource_type == "script")
                    or (origin == "https://lh3.googleusercontent.com" and resource_type == "image"))
        except Exception:
            return False

    def _set_route_origins(self, origins: list[str]) -> frozenset[str]:
        normalized = _parse_allowlist(origins)
        self._route_origins = normalized
        return normalized

    async def _classify_click(self, x: float, y: float) -> dict[str, Any]:
        # Resolve nested text/SVG hits to the nearest semantic control. Model-provided
        # code is never evaluated; this fixed script only reads page structure.
        result = await self._page.evaluate(
            """({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return {tag: "", role: "", type: "", href: "", target: "", editable: false};
                const selector = 'a,area,button,input,textarea,select,label,[role="button"],[role="link"],' +
                    '[role="textbox"],[role="combobox"],[role="checkbox"],[role="radio"],[contenteditable="true"]';
                let target = el.closest(selector) || el;
                if (target.tagName.toLowerCase() === "label") {
                    const control = target.control;
                    if (control && control.matches("input,textarea,select,button")) target = control;
                }
                return {tag: target.tagName.toLowerCase(), role: (target.getAttribute("role") || "").toLowerCase(),
                    type: (target.getAttribute("type") || "").toLowerCase(),
                    href: target.matches("a,area") ? (target.href || "") : "",
                    target: target.matches("a,area") ? (target.getAttribute("target") || "").toLowerCase() : "",
                    editable: !!target.isContentEditable || target.matches('[contenteditable="true"]')};
            }""",
            {"x": x, "y": y},
        )
        return result if isinstance(result, dict) else {}

    async def _is_textarea_resize_corner(self, x: float, y: float) -> bool:
        # Fixed read-only hit test. Limit agent drags to the native resize grip.
        return await self._page.evaluate(
            """({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el || el.tagName !== 'TEXTAREA' || el.disabled) return false;
                const style = getComputedStyle(el), box = el.getBoundingClientRect();
                if (!['both', 'horizontal', 'vertical'].includes(style.resize)) return false;
                const onSide = style.direction === 'rtl' ? x - box.left : box.right - x;
                return onSide >= 0 && onSide <= 16 && box.bottom - y >= 0 && box.bottom - y <= 16;
            }""", {"x": x, "y": y}) is True

    @staticmethod
    def _validate_action(action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if not isinstance(action, dict) or not isinstance(action.get("kind"), str):
            raise WorkerError("INVALID_ACTION", "action must be a typed object with kind", status_code=422)
        kind = action["kind"]
        common_allowed: dict[str, set[str]] = {
            "click": {"kind", "x", "y", "button", "count"},
            "mouse_move": {"kind", "x", "y"},
            "drag": {"kind", "x", "y"},
            "type_text": {"kind", "text"}, "gemini_submit_prompt": {"kind", "text"},
            "press_key": {"kind", "key"},
            "scroll": {"kind", "dx", "dy", "x", "y"}, "navigate": {"kind", "url"},
            "gemini_refresh_session": {"kind"},
            "history_back": {"kind"}, "wait": {"kind", "seconds"},
        }
        if kind not in common_allowed or set(action) - common_allowed[kind]:
            raise WorkerError("INVALID_ACTION", "unknown action kind or extra action fields", status_code=422)
        if kind in {"click", "mouse_move", "drag", "scroll"}:
            for key in (("x", "y") if kind in {"click", "mouse_move", "drag"} else ("dx", "dy")):
                value = action.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math_isfinite(value):
                    raise WorkerError("INVALID_ACTION", f"{key} must be finite", status_code=422)
            if kind in {"click", "mouse_move", "drag"}:
                if not 0 <= action["x"] < VIEWPORT["width"] or not 0 <= action["y"] < VIEWPORT["height"]:
                    raise WorkerError("INVALID_ACTION", "pointer position is outside the current viewport", status_code=422)
            if kind == "click":
                count = action.get("count", 1)
                if action.get("button", "left") not in {"left", "right"} or isinstance(count, bool) or not isinstance(count, int) or count not in {1, 2, 3}:
                    raise WorkerError("INVALID_ACTION", "unsupported click button or count", status_code=422)
            elif kind == "scroll" and max(abs(action["dx"]), abs(action["dy"])) > 1000:
                raise WorkerError("INVALID_ACTION", "scroll delta exceeds 1000", status_code=422)
        if kind in {"type_text", "gemini_submit_prompt"} and (
                not isinstance(action.get("text"), str) or len(action["text"]) > MAX_ACTION_TEXT):
            raise WorkerError("INVALID_ACTION", "text must be a string of at most 100000 characters", status_code=422)
        if kind == "gemini_submit_prompt" and not action["text"]:
            raise WorkerError("INVALID_ACTION", "Gemini prompt must not be empty", status_code=422)
        if kind == "press_key" and (not isinstance(action.get("key"), str) or len(action["key"]) > 100):
            raise WorkerError("INVALID_ACTION", "key must be a string of at most 100 characters", status_code=422)
        if kind == "wait" and (isinstance(action.get("seconds"), bool) or not isinstance(action.get("seconds"), (int, float)) or not math_isfinite(action["seconds"]) or not 0 <= action["seconds"] <= 10):
            raise WorkerError("INVALID_ACTION", "wait seconds must be between 0 and 10", status_code=422)
        if kind == "navigate":
            _, _ = _safe_url(action.get("url"))
        return kind, action

    async def _execute_action(self, action: dict[str, Any], allowed: frozenset[str], *, manual: bool, allow_write: bool,
                              expected_epoch: int, site_capability: SiteCapability | None = None,
                              action_id: str | None = None) -> dict[str, Any]:
        kind = action.get("kind") if isinstance(action, dict) else None
        if not (ReadinessActionTrace.enabled() and kind in {"click", "type_text", "press_key", "scroll", "wait", "navigate", "drag", "mouse_move"}):
            return await self._execute_action_untraced(action, allowed, manual=manual, allow_write=allow_write,
                expected_epoch=expected_epoch, site_capability=site_capability, action_id=action_id)
        try:
            current = urlsplit(await self._adapter.url())
            trace_paths = {"/checkbox", "/webtables", "/text-box", "/buttons", "/dynamic-properties", "/tree", "/form", "/overlay"}
            trace_page = current.scheme == "https" and current.path.rstrip("/") in trace_paths and current.hostname in {"demoqa.com", "readiness-fixture.test"}
        except Exception:
            trace_page = False
        if not trace_page:
            return await self._execute_action_untraced(action, allowed, manual=manual, allow_write=allow_write,
                expected_epoch=expected_epoch, site_capability=site_capability, action_id=action_id)
        if kind == "click" and any(type(action.get(key)) not in (int, float) or not math_isfinite(action.get(key)) for key in ("x", "y")):
            return await self._execute_action_untraced(action, allowed, manual=manual, allow_write=allow_write,
                expected_epoch=expected_epoch, site_capability=site_capability, action_id=action_id)
        started = time.monotonic()
        record: dict[str, Any] = {"kind": kind, "viewport": {"width": self.viewport_width, "height": self.viewport_height}}
        if kind in {"click", "mouse_move", "drag"}:
            for key in ("x", "y"):
                value = action.get(key)
                if type(value) in (int, float): record[key] = value
        async def snapshot(x: Any = None, y: Any = None) -> dict[str, Any]:
            try:
                result = await asyncio.wait_for(self._page.evaluate(READINESS_ACTION_SNAPSHOT, {"x": x, "y": y}), timeout=1)
                return result if isinstance(result, dict) else {}
            except Exception: return {}
        before = await snapshot(float(action["x"]), float(action["y"])) if kind == "click" else await snapshot()
        record["focus_before"] = before.get("focus", "unknown")
        record["target_before"] = before.get("focus_target", "unknown")
        if isinstance(before.get("bounds"), dict): record["target_boxes"] = before["bounds"]
        if kind == "click":
            for key in ("hit", "target", "bbox"):
                if key in before: record[key] = before[key]
        if isinstance(before.get("checks"), dict): record["checked_before"] = before["checks"]
        if isinstance(before.get("filled"), dict): record["filled_before"] = before["filled"]
        outcome = "applied"
        try:
            return await self._execute_action_untraced(action, allowed, manual=manual, allow_write=allow_write,
                expected_epoch=expected_epoch, site_capability=site_capability, action_id=action_id)
        except WorkerError as exc:
            outcome = "unknown" if exc.code in {"ACTION_OUTCOME_UNKNOWN", "STALE_CONTROL_EPOCH"} else "rejected"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            after = await snapshot()
            record["focus_after"] = after.get("focus", "unknown")
            record["target_after"] = after.get("focus_target", "unknown")
            if isinstance(after.get("checks"), dict): record["checked_after"] = after["checks"]
            if isinstance(after.get("filled"), dict): record["filled_after"] = after["filled"]
            record["elapsed_ms"] = (time.monotonic() - started) * 1000
            record["outcome"] = outcome
            try: self._readiness_trace.append(record)
            except Exception: pass

    async def _execute_action_untraced(self, action: dict[str, Any], allowed: frozenset[str], *, manual: bool, allow_write: bool,
                              expected_epoch: int, site_capability: SiteCapability | None = None,
                              action_id: str | None = None) -> dict[str, Any]:
        kind, action = self._validate_action(action)
        if kind == "gemini_refresh_session":
            from worker.site_capabilities import GEMINI_ORIGIN, GEMINI_SESSION_REFRESH_PREFLIGHT

            if (manual or allow_write or site_capability is not None
                    or allowed != frozenset({GEMINI_ORIGIN})):
                raise WorkerError("POLICY_DENIED", "Gemini session refresh requires read-only Gemini-only agent control")
            if not self.egress_ready or self.route_generation == 0:
                raise WorkerError("EGRESS_NOT_READY", "live browsing is disabled until the external egress route is verified", status_code=503)
            async with self._input_lock:
                self._check_agent_gate(expected_epoch)
                try:
                    current_url = await self._adapter.url()
                    if _origin(current_url) != GEMINI_ORIGIN:
                        raise WorkerError("POLICY_DENIED", "Gemini session refresh is restricted to the current Gemini page")
                    probe = await asyncio.wait_for(self._page.evaluate(GEMINI_SESSION_REFRESH_PREFLIGHT), timeout=2)
                except WorkerError:
                    raise
                except Exception as exc:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini page safety could not be confirmed") from exc
                if (not isinstance(probe, dict) or set(probe) != {"safe_to_refresh", "reason_code"}
                        or type(probe.get("safe_to_refresh")) is not bool
                        or probe.get("reason_code") not in {"EMPTY_COMPOSER", "NOT_GEMINI", "MODAL_PRESENT",
                                                            "COMPOSER_UNAVAILABLE", "DRAFT_NOT_EMPTY"}):
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini page safety could not be confirmed")
                if probe["safe_to_refresh"] is not True:
                    if probe["reason_code"] == "DRAFT_NOT_EMPTY":
                        raise WorkerError("DRAFT_NOT_EMPTY", "Gemini has an existing draft; it was preserved and the page was not refreshed", status_code=409)
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini page is not in a safe state to refresh")
                self._check_agent_gate(expected_epoch)
                self._route_origins = allowed
                self._blocked_resources.clear()
                try:
                    await self._adapter.goto(current_url, wait_until="domcontentloaded", timeout_ms=30_000)
                    self._check_agent_gate(expected_epoch)
                    if _origin(await self._adapter.url()) != GEMINI_ORIGIN:
                        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "Gemini session refresh left the allowed origin")
                except WorkerError:
                    raise
                except Exception as exc:
                    raise WorkerError("ACTION_OUTCOME_UNKNOWN", "Gemini session refresh outcome is unknown") from exc
            return {"origin": GEMINI_ORIGIN, "session_refreshed": True}
        if kind == "gemini_submit_prompt":
            from worker.site_capabilities import (
                GEMINI_ORIGIN, GEMINI_PROMPT_ACK, GEMINI_PROMPT_PREFLIGHT,
                GEMINI_PROMPT_FOCUS_VERIFY, GEMINI_PROMPT_READY,
                GEMINI_PROMPT_SEND_TARGET, GEMINI_PROMPT_VERIFY, is_gemini_submit,
            )

            # This operation is intentionally stricter than normal autonomous
            # typing: it is only the explicitly approved Gemini submission
            # workflow, with one exact origin and a one-use capability.
            if (manual or not allow_write or site_capability is None
                    or site_capability.kind != "gemini_prompt_submit"
                    or allowed != frozenset({GEMINI_ORIGIN})
                    or not action["text"]):
                raise WorkerError("POLICY_DENIED", "Gemini prompt workflow requirements were not met")
            if not action_id or self._db is None:
                raise WorkerError("POLICY_DENIED", "Gemini prompt submission requires an action ledger")

            async with self._input_lock:
                started = time.monotonic()
                editor_deadline = started + GEMINI_PROMPT_EDITOR_TIMEOUT_SECONDS
                pre_submit_deadline = editor_deadline
                timings = {"preflight": 0, "insert": 0, "verify": 0, "submit": 0, "ack": 0, "total": 0}

                def elapsed_ms() -> int:
                    return max(0, round((time.monotonic() - started) * 1000))

                async def inspect_before_submit(script: str, arg: Any = None) -> Any:
                    remaining = pre_submit_deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt checks exceeded the time limit")
                    try:
                        call = self._page.evaluate(script) if arg is None else self._page.evaluate(script, arg)
                        return await asyncio.wait_for(call, timeout=remaining)
                    except asyncio.TimeoutError as exc:
                        raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt checks exceeded the time limit") from exc

                self._check_agent_gate(expected_epoch)
                try:
                    current_url = await self._adapter.url()
                    current_origin = _origin(current_url)
                except Exception as exc:
                    raise WorkerError("POLICY_DENIED", "Gemini prompt requires the canonical Gemini page") from exc
                if current_origin != GEMINI_ORIGIN or GEMINI_ORIGIN not in self._route_origins:
                    raise WorkerError("POLICY_DENIED", "Gemini prompt requires the canonical Gemini page")
                self._check_agent_gate(expected_epoch)
                await self._require_gemini_auth()
                self._check_agent_gate(expected_epoch)
                used = self._db.execute("SELECT 1 FROM capability_uses WHERE grant_id=?",
                                        (site_capability.grant_id,)).fetchone()
                if used is not None:
                    raise WorkerError("CAPABILITY_ALREADY_USED", "submission capability was already used")

                phase = time.monotonic()
                try:
                    probe = await inspect_before_submit(GEMINI_PROMPT_PREFLIGHT, {"text": action["text"]})
                except Exception as exc:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor could not be inspected") from exc
                while probe == "editor_not_ready":
                    self._check_agent_gate(expected_epoch)
                    if time.monotonic() >= editor_deadline:
                        raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor did not become ready")
                    await asyncio.sleep(min(GEMINI_PROMPT_EDITOR_POLL_SECONDS,
                                            max(0.0, editor_deadline - time.monotonic())))
                    try:
                        probe = await inspect_before_submit(GEMINI_PROMPT_PREFLIGHT, {"text": action["text"]})
                    except Exception as exc:
                        raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor could not be inspected") from exc
                timings["preflight"] = max(0, round((time.monotonic() - phase) * 1000))
                self._check_agent_gate(expected_epoch)
                pre_submit_deadline = time.monotonic() + GEMINI_PROMPT_PRE_SUBMIT_TIMEOUT_SECONDS
                if probe == "draft_mismatch":
                    raise WorkerError("DRAFT_NOT_EMPTY", "Gemini prompt editor contains different text")
                if not isinstance(probe, dict) or set(probe) != {"state", "x", "y"}:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor is not in a safe state")
                state = probe.get("state")
                x, y = probe.get("x"), probe.get("y")
                if (isinstance(x, bool) or not isinstance(x, (int, float)) or not math_isfinite(x)
                        or isinstance(y, bool) or not isinstance(y, (int, float)) or not math_isfinite(y)
                        or not 0 <= x < self.viewport_width or not 0 <= y < self.viewport_height):
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt focus target is invalid")
                if state not in {"empty", "same"}:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor is not in a safe state")

                # page.evaluate runs in a read-only Camoufox isolated world.
                # Focus is a real pointer action, then verified by the fixed
                # read-only predicate before native text input.
                self._check_agent_gate(expected_epoch)
                phase = time.monotonic()
                try:
                    remaining = pre_submit_deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    self._pointer_position = None
                    await asyncio.wait_for(self._page.mouse.click(float(x), float(y), button="left", click_count=1),
                                           timeout=remaining)
                    self._pointer_position = (float(x), float(y))
                except Exception as exc:
                    self._pointer_position = None
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor could not be focused") from exc
                timings["preflight"] += max(0, round((time.monotonic() - phase) * 1000))
                self._check_agent_gate(expected_epoch)
                try:
                    focused = await inspect_before_submit(GEMINI_PROMPT_FOCUS_VERIFY)
                except Exception as exc:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor focus could not be verified") from exc
                self._check_agent_gate(expected_epoch)
                if focused is not True:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt editor is not focused")

                if state == "empty":
                    phase = time.monotonic()
                    try:
                        remaining = pre_submit_deadline - time.monotonic()
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        await asyncio.wait_for(self._page.keyboard.insert_text(action["text"]), timeout=remaining)
                    except asyncio.TimeoutError as exc:
                        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "prompt text entry outcome is unknown") from exc
                    except Exception as exc:
                        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "prompt text entry outcome is unknown") from exc
                    timings["insert"] = max(0, round((time.monotonic() - phase) * 1000))
                    self._check_agent_gate(expected_epoch)

                phase = time.monotonic()
                try:
                    matches = await inspect_before_submit(GEMINI_PROMPT_VERIFY, {"text": action["text"]})
                except Exception as exc:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt text could not be verified") from exc
                timings["verify"] = max(0, round((time.monotonic() - phase) * 1000))
                self._check_agent_gate(expected_epoch)
                if matches is not True:
                    raise WorkerError("PROMPT_TEXT_MISMATCH", "Gemini prompt text did not match the supplied value")

                # The prompt is private and only compared in-page. It is never
                # included in a worker result, event, error, or receipt.
                ready_deadline = time.monotonic() + GEMINI_PROMPT_READY_TIMEOUT_SECONDS
                while True:
                    self._check_agent_gate(expected_epoch)
                    if _origin(await self._adapter.url()) != GEMINI_ORIGIN:
                        raise WorkerError("POLICY_DENIED", "page origin changed")
                    phase = time.monotonic()
                    try:
                        ready = await inspect_before_submit(GEMINI_PROMPT_READY)
                    except Exception as exc:
                        raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini submit control could not be inspected") from exc
                    timings["preflight"] += max(0, round((time.monotonic() - phase) * 1000))
                    self._check_agent_gate(expected_epoch)
                    if ready is True:
                        break
                    if time.monotonic() >= ready_deadline:
                        raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini submit control is not ready")
                    await asyncio.sleep(min(GEMINI_PROMPT_READY_POLL_SECONDS,
                                            max(0.0, ready_deadline - time.monotonic())))
                remaining = pre_submit_deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt checks exceeded the time limit")
                try:
                    target = await asyncio.wait_for(self._page.evaluate(GEMINI_PROMPT_SEND_TARGET),
                                                    timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt checks exceeded the time limit") from exc
                if (not isinstance(target, dict) or set(target) != {"x", "y"}
                        or isinstance(target.get("x"), bool) or not isinstance(target.get("x"), (int, float))
                        or isinstance(target.get("y"), bool) or not isinstance(target.get("y"), (int, float))
                        or not 0 <= target["x"] < self.viewport_width
                        or not 0 <= target["y"] < self.viewport_height):
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini submit control is not ready")
                try:
                    submit_check = await asyncio.wait_for(
                        is_gemini_submit(self._page, {"kind": "click", "x": target["x"], "y": target["y"]}),
                        timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini prompt checks exceeded the time limit") from exc
                if not submit_check:
                    raise WorkerError("CAPABILITY_UNAVAILABLE", "Gemini submit control is not ready")
                self._check_agent_gate(expected_epoch)
                if _origin(await self._adapter.url()) != GEMINI_ORIGIN:
                    raise WorkerError("POLICY_DENIED", "page origin changed")
                await self._require_gemini_auth()
                self._check_agent_gate(expected_epoch)

                # Consume immediately before the one external submission. If
                # anything after this point is uncertain, callers must recover
                # by inspection and may never replay under another action ID.
                self._check_agent_gate(expected_epoch)
                self._claim_capability(site_capability.grant_id, action_id)
                try:
                    phase = time.monotonic()
                    self._pointer_position = None
                    await asyncio.wait_for(self._page.mouse.click(float(target["x"]), float(target["y"]),
                                                                 button="left", click_count=1),
                                           timeout=GEMINI_PROMPT_ENTER_TIMEOUT_SECONDS)
                    self._pointer_position = (float(target["x"]), float(target["y"]))
                    timings["submit"] = max(0, round((time.monotonic() - phase) * 1000))
                    self._check_agent_gate(expected_epoch)
                    # A cleared composer is a bounded acknowledgement that the
                    # page accepted the submit gesture. It does not imply that
                    # a response was generated or that any response text is trusted.
                    phase = time.monotonic()
                    deadline = phase + GEMINI_PROMPT_ACK_TIMEOUT_SECONDS
                    acknowledged = False
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            acknowledged = await asyncio.wait_for(self._page.evaluate(GEMINI_PROMPT_ACK),
                                                                  timeout=remaining)
                        except Exception:
                            acknowledged = False
                            break
                        if acknowledged or time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(min(GEMINI_PROMPT_ACK_POLL_SECONDS,
                                                max(0.0, deadline - time.monotonic())))
                    timings["ack"] = max(0, round((time.monotonic() - phase) * 1000))
                    timings["total"] = elapsed_ms()
                    if not acknowledged:
                        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "Gemini accepted-submit acknowledgement was not observed")
                    self._check_agent_gate(expected_epoch)
                    return {"submitted": True, "submit_dispatched": True, "submit_method": "click",
                            "enter_dispatched": False,
                            "submission_acknowledged": True, "response_verified": False,
                            "ack_kind": "composer_cleared", "input_verified": True,
                            "characters": len(action["text"]), "timings_ms": timings}
                except asyncio.CancelledError as exc:
                    raise WorkerError("ACTION_OUTCOME_UNKNOWN", "Gemini submission outcome is unknown") from exc
                except Exception as exc:
                    if isinstance(exc, WorkerError) and exc.code == "ACTION_OUTCOME_UNKNOWN":
                        raise
                    raise WorkerError("ACTION_OUTCOME_UNKNOWN", "Gemini submission outcome is unknown") from exc

        if not manual and kind in {"click", "press_key", "drag", "type_text"}:
            from worker.site_capabilities import GEMINI_ORIGIN, is_gemini_submit

            try:
                current_origin = _origin(await self._adapter.url())
            except WorkerError:
                current_origin = None
            needs_auth = current_origin == GEMINI_ORIGIN and allow_write
            if (not needs_auth and current_origin == GEMINI_ORIGIN and not allow_write
                    and site_capability is not None and site_capability.kind == "gemini_prompt_submit"
                    and kind in {"click", "press_key"}):
                needs_auth = await is_gemini_submit(self._page, action)
            if needs_auth:
                await self._require_gemini_auth()

        if not manual and not allow_write and site_capability and kind in {"click", "press_key"}:
            from worker.site_capabilities import GEMINI_ORIGIN, is_gemini_submit

            async with self._input_lock:
                self._check_agent_gate(expected_epoch)
                try:
                    current_origin = _origin(await self._adapter.url())
                except WorkerError:
                    current_origin = None
                if (GEMINI_ORIGIN in allowed and current_origin == GEMINI_ORIGIN
                        and await is_gemini_submit(self._page, action)):
                    self._check_agent_gate(expected_epoch)
                    if _origin(await self._adapter.url()) != GEMINI_ORIGIN:
                        raise WorkerError("POLICY_DENIED", "page origin changed")
                    self._check_agent_gate(expected_epoch)
                    self._claim_capability(site_capability.grant_id, action_id)
                    try:
                        if kind == "click":
                            x, y = float(action["x"]), float(action["y"])
                            self._pointer_position = None
                            await self._page.mouse.click(x, y, button="left", click_count=1)
                            self._pointer_position = (x, y)
                            result = {"x": x, "y": y, "button": "left", "count": 1}
                        else:
                            await self._page.keyboard.press("Enter")
                            result = {"key": "Enter"}
                        self._check_agent_gate(expected_epoch)
                    except Exception as exc:
                        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "submission outcome unknown") from exc
                    return result
        if kind == "navigate":
            if not self.egress_ready or self.route_generation == 0:
                raise WorkerError("EGRESS_NOT_READY", "live browsing is disabled until the external egress route is verified", status_code=503)
            url, origin = _safe_url(action["url"])
            if not allowed or origin not in allowed:
                raise WorkerError("POLICY_DENIED", "navigation origin is not in allowed_origins")
            self._route_origins = allowed
            self._blocked_resources.clear()
            nav = await self._adapter.goto(url, wait_until="domcontentloaded", timeout_ms=30_000)
            final_url = nav.get("url", await self._adapter.url())
            final_origin = _origin(final_url)
            if final_origin not in allowed:
                raise WorkerError("POLICY_DENIED", "navigation ended outside allowed_origins")
            return {"url": final_url, "status": nav.get("status")}
        if kind == "click":
            x, y = float(action["x"]), float(action["y"])
            target = await self._classify_click(x, y)
            tag, role, input_type = target.get("tag", ""), target.get("role", ""), target.get("type", "")
            if not allow_write and (tag == "button" or role == "button" or tag == "input" and input_type in {"submit", "button", "image"}):
                raise WorkerError("APPROVAL_REQUIRED", "button-like target may submit or change external state")
            editable = bool(target.get("editable")) or tag in {"textarea", "select", "option"} or (tag == "input" and input_type not in {"submit", "button", "image", "file", "hidden"}) or role in {"textbox", "combobox", "checkbox", "radio"}
            if not allow_write and not editable and (not tag or tag not in {"a", "area"} and role not in {"link"}):
                raise WorkerError("APPROVAL_REQUIRED", "click target is not a recognized link or form control")
            if tag in {"a", "area"} or role == "link":
                href = target.get("href", "")
                if href:
                    try:
                        dest = _origin(href)
                    except WorkerError as exc:
                        raise WorkerError("POLICY_DENIED", "link destination is not an allowed HTTPS URL") from exc
                    if dest not in allowed:
                        raise WorkerError("POLICY_DENIED", "link destination is outside allowed_origins")
                    # A recognized new-tab link is followed in the canonical tab.
                    # This avoids creating a usable second page while retaining normal
                    # typed click policy and navigation fencing.
                    if target.get("target") == "_blank" and action.get("button", "left") == "left" and action.get("count", 1) == 1:
                        async with self._input_lock:
                            self._check_manual_or_agent(manual, expected_epoch)
                            self._route_origins = allowed
                            self._blocked_resources.clear()
                            nav = await self._adapter.goto(href, wait_until="domcontentloaded", timeout_ms=30_000)
                            self._check_manual_or_agent(manual, expected_epoch)
                        final_url = nav.get("url", await self._adapter.url())
                        if _origin(final_url) not in allowed:
                            raise WorkerError("POLICY_DENIED", "navigation ended outside allowed_origins")
                        return {"url": final_url, "status": nav.get("status"), "opened_in_current_page": True}
            async with self._input_lock:
                self._check_manual_or_agent(manual, expected_epoch)
                await self._require_auth_for_write(manual=manual, allow_write=allow_write, kind=kind)
                self._pointer_position = None
                await self._page.mouse.click(x, y, button=action.get("button", "left"), click_count=action.get("count", 1))
                self._pointer_position = (x, y)
                self._check_manual_or_agent(manual, expected_epoch)
            return {"x": x, "y": y, "button": action.get("button", "left"), "count": action.get("count", 1)}
        if kind == "mouse_move":
            async with self._input_lock:
                self._check_manual_or_agent(manual, expected_epoch)
                self._pointer_position = None
                await self._page.mouse.move(float(action["x"]), float(action["y"]))
                self._pointer_position = (float(action["x"]), float(action["y"]))
                self._check_manual_or_agent(manual, expected_epoch)
            return {"x": action["x"], "y": action["y"]}
        if kind == "drag":
            x, y = float(action["x"]), float(action["y"])
            async with self._input_lock:
                self._check_manual_or_agent(manual, expected_epoch)
                await self._require_auth_for_write(manual=manual, allow_write=allow_write, kind=kind)
                start = self._pointer_position
                if start is None:
                    raise WorkerError("DRAG_START_UNKNOWN", "drag requires a known pointer position")
                if not manual and not await self._is_textarea_resize_corner(*start):
                    raise WorkerError("APPROVAL_REQUIRED", "agent drag requires a textarea resize corner")
                self._check_manual_or_agent(manual, expected_epoch)
                started = False
                try:
                    async with asyncio.timeout(1):
                        started = True  # down may have reached the browser even if it raises
                        await self._page.mouse.down(button="left")
                        for step in range(1, 11):
                            self._check_manual_or_agent(manual, expected_epoch)
                            px = start[0] + (x - start[0]) * step / 10
                            py = start[1] + (y - start[1]) * step / 10
                            self._pointer_position = None
                            await self._page.mouse.move(px, py)
                            self._pointer_position = (px, py)
                        self._check_manual_or_agent(manual, expected_epoch)
                except WorkerError as exc:
                    if started and exc.code != "STALE_CONTROL_EPOCH":
                        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "drag may have started") from exc
                    raise
                finally:
                    if started:
                        try:
                            async with asyncio.timeout(0.4):
                                await self._page.mouse.up(button="left")
                        except (Exception, asyncio.CancelledError) as exc:
                            self._pointer_position = None
                            self._trip_resource_violation("mouse release failed; restart required")
                            raise WorkerError("ACTION_OUTCOME_UNKNOWN", "mouse release could not be confirmed") from exc
                try:
                    self._check_manual_or_agent(manual, expected_epoch)
                except WorkerError as exc:
                    if exc.code != "STALE_CONTROL_EPOCH":
                        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "drag may have completed") from exc
                    raise
            return {"x": x, "y": y}
        if kind == "type_text":
            focused = await self._page.evaluate("() => { const e = document.activeElement; return !!e && (e.isContentEditable || /^(INPUT|TEXTAREA)$/.test(e.tagName)); }")
            if not focused:
                raise WorkerError("CAPABILITY_UNAVAILABLE", "text input requires a focused editable field")
            async with self._input_lock:
                self._check_manual_or_agent(manual, expected_epoch)
                await self._require_auth_for_write(manual=manual, allow_write=allow_write, kind=kind)
                await self._page.keyboard.insert_text(action["text"])
                self._check_manual_or_agent(manual, expected_epoch)
            return {"characters": len(action["text"])}
        if kind == "press_key":
            key = action["key"]
            parts = key.split("+")
            if not parts or any(part not in _KEYS for part in parts) or len(set(parts)) != len(parts):
                raise WorkerError("INVALID_ACTION", "key is not in the worker allowlist", status_code=422)
            if any(mod not in _MODIFIERS for mod in parts[:-1]):
                raise WorkerError("INVALID_ACTION", "key chords must use allowlisted modifiers followed by one key", status_code=422)
            is_enter = parts[-1] == "Enter"
            if is_enter and not (manual or allow_write):
                raise WorkerError("APPROVAL_REQUIRED", "Enter can submit a form and requires human or approved control")
            async with self._input_lock:
                self._check_manual_or_agent(manual, expected_epoch)
                await self._require_auth_for_write(manual=manual, allow_write=allow_write, kind=kind)
                await self._page.keyboard.press(key)
                self._check_manual_or_agent(manual, expected_epoch)
            return {"key": key}
        if kind == "scroll":
            dx, dy = float(action["dx"]), float(action["dy"])
            if ("x" in action) != ("y" in action):
                raise WorkerError("INVALID_ACTION", "scroll position requires both x and y", status_code=422)
            async with self._input_lock:
                self._check_manual_or_agent(manual, expected_epoch)
                if "x" in action:
                    x, y = action["x"], action["y"]
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math_isfinite(v) for v in (x, y)) or not 0 <= x < VIEWPORT["width"] or not 0 <= y < VIEWPORT["height"]:
                        raise WorkerError("INVALID_ACTION", "scroll position is outside the viewport", status_code=422)
                    self._pointer_position = None
                    await self._page.mouse.move(x, y)
                    self._pointer_position = (float(x), float(y))
                await self._page.mouse.wheel(dx, dy)
                self._check_manual_or_agent(manual, expected_epoch)
            return {"dx": dx, "dy": dy}
        if kind == "history_back":
            async with self._input_lock:
                self._check_manual_or_agent(manual, expected_epoch)
                await self._page.go_back(wait_until="domcontentloaded", timeout=30_000)
                self._check_manual_or_agent(manual, expected_epoch)
            return {"url": await self._adapter.url()}
        if kind == "wait":
            start_epoch = expected_epoch
            await asyncio.sleep(float(action["seconds"]))
            if self.control_epoch != start_epoch:
                raise WorkerError("STALE_CONTROL_EPOCH", "control changed while waiting")
            return {"seconds": float(action["seconds"])}
        raise WorkerError("INVALID_ACTION", "unsupported action kind", status_code=422)

    def _check_manual_or_agent(self, manual: bool, expected_epoch: int) -> None:
        self._check_epoch(expected_epoch)
        if manual:
            if self.control_owner != "human":
                raise WorkerError("CONTROL_NOT_HUMAN", "manual input is not the current owner")
        else:
            self._check_agent_gate(expected_epoch)

    def _ledger_claim(self, action_id: str, fingerprint: str) -> dict[str, Any] | None:
        assert self._db is not None
        self._db.execute("BEGIN IMMEDIATE")
        try:
            result = self._ledger_claim_transaction(action_id, fingerprint)
            self._db.execute("COMMIT")
            return result
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def _ledger_claim_transaction(self, action_id: str, fingerprint: str) -> dict[str, Any] | None:
        assert self._db is not None
        row = self._db.execute("SELECT fingerprint,state,receipt_json FROM action_ledger WHERE action_id=?", (action_id,)).fetchone()
        if row is not None:
            if row[0] != fingerprint:
                raise WorkerError("ACTION_ID_CONFLICT", "action_id was reused with different arguments")
            if row[2]:
                return json.loads(row[2])
            return {"outcome": "unknown", "action_id": action_id, "error": "INTERRUPTED_AFTER_START"}
        now = _utc_now()
        self._db.execute("INSERT INTO action_ledger(action_id,fingerprint,state,created_at,updated_at) VALUES(?,?, 'started',?,?)", (action_id, fingerprint, now, now))
        return None

    def _ledger_lookup(self, action_id: str, fingerprint: str) -> dict[str, Any] | None:
        assert self._db is not None
        row = self._db.execute("SELECT fingerprint,state,receipt_json FROM action_ledger WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise WorkerError("ACTION_ID_CONFLICT", "action_id was reused with different arguments")
        if row[2]:
            return json.loads(row[2])
        return {"outcome": "unknown", "action_id": action_id, "error": "INTERRUPTED_AFTER_START"}

    def _ledger_finish(self, action_id: str, receipt: dict[str, Any]) -> None:
        assert self._db is not None
        self._db.execute("UPDATE action_ledger SET state=?,receipt_json=?,updated_at=? WHERE action_id=?",
                         (receipt["outcome"], _json(receipt), _utc_now(), action_id))

    def _claim_capability(self, grant_id: str, action_id: str | None) -> None:
        if not action_id or self._db is None:
            raise WorkerError("POLICY_DENIED", "submission capability requires an action ledger")
        try:
            self._db.execute("INSERT INTO capability_uses(grant_id,action_id,state,created_at) VALUES(?,?,'started',?)",
                             (grant_id, action_id, _utc_now()))
        except sqlite3.IntegrityError as exc:
            raise WorkerError("CAPABILITY_ALREADY_USED", "submission capability was already used") from exc

    async def execute_agent(self, body: AgentExecuteBody) -> dict[str, Any]:
        if not isinstance(body.allow_write, bool):
            raise WorkerError("INVALID_ACTION", "allow_write must be boolean", status_code=422)
        allowed = _parse_allowlist(body.allowed_origins)
        fingerprint_data = {"action": body.action, "observation_id": body.observation_id,
                            "control_epoch": body.control_epoch, "allowed": sorted(allowed),
                            "allow_write": body.allow_write}
        if body.site_capability is not None:
            fingerprint_data["site_capability"] = body.site_capability.model_dump()
        fingerprint = hashlib.sha256(_json(fingerprint_data).encode()).hexdigest()
        cached = self._ledger_lookup(body.action_id, fingerprint)
        if cached is not None:
            return cached
        self._check_agent_gate(body.control_epoch)
        if body.observation_id != self._last_observation_id:
            raise WorkerError("STALE_OBSERVATION", "observation_id is not current")
        if self._action_in_progress or self._import_in_progress:
            raise WorkerError("CONTROL_BUSY", "another browser operation is in progress")
        cached = self._ledger_claim(body.action_id, fingerprint)
        if cached is not None:
            return cached
        self._last_observation_id = None
        self._route_origins = allowed
        self._action_in_progress = True
        result = None
        try:
            self._check_agent_gate(body.control_epoch)
            result = await self._execute_action(body.action, allowed, manual=False, allow_write=body.allow_write,
                                                expected_epoch=body.control_epoch, site_capability=body.site_capability,
                                                action_id=body.action_id)
            self._check_agent_gate(body.control_epoch)
            receipt = {"outcome": "applied", "action_id": body.action_id, "result": result}
        except WorkerError as exc:
            outcome = "unknown" if exc.code in {"STALE_CONTROL_EPOCH", "ACTION_OUTCOME_UNKNOWN"} or (body.action.get("kind") == "drag" and result is not None) else "rejected"
            receipt = {"outcome": outcome, "action_id": body.action_id, "error": exc.code}
            self._ledger_finish(body.action_id, receipt)
            if exc.code == "ACTION_OUTCOME_UNKNOWN":
                return receipt
            raise
        except Exception:
            receipt = {"outcome": "unknown", "action_id": body.action_id, "error": "ACTION_OUTCOME_UNKNOWN"}
            self._ledger_finish(body.action_id, receipt)
            return receipt
        finally:
            self._action_in_progress = False
        self._ledger_finish(body.action_id, receipt)
        return receipt

    async def execute_manual(self, body: ManualExecuteBody) -> dict[str, Any]:
        self._check_epoch(body.control_epoch)
        if self.control_owner != "human":
            raise WorkerError("CONTROL_NOT_HUMAN", "manual input requires human control")
        if not self._started or self._page is None or self._adapter is None:
            raise WorkerError("BROWSER_UNAVAILABLE", "browser worker is not ready", status_code=503)
        action_id = f"manual-{uuid.uuid4()}"
        if body.allowed_origins is None:
            current_url = await self._adapter.url()
            try:
                origins = frozenset({_origin(current_url)})
            except WorkerError:
                origins = frozenset()
        else:
            origins = _parse_allowlist(body.allowed_origins)
        kind, action = self._validate_action(body.action)
        if kind == "navigate" and not origins:
            _, origin = _safe_url(action["url"])
            origins = frozenset({origin})
        if kind == "navigate" and (not self.egress_ready or self.route_generation == 0):
            raise WorkerError("EGRESS_NOT_READY", "manual browsing is disabled until the external egress route is configured", status_code=503)
        start_epoch = self.control_epoch
        if self._action_in_progress:
            raise WorkerError("ACTION_IN_PROGRESS", "another input action is running")
        self._route_origins = origins
        self._action_in_progress = True
        try:
            result = await self._execute_action(action, origins, manual=True, allow_write=True, expected_epoch=start_epoch)
        except WorkerError as exc:
            if exc.code == "ACTION_OUTCOME_UNKNOWN":
                self._last_observation_id = None
                return {"outcome": "unknown", "action_id": action_id, **self.status_base(), "error": exc.code}
            raise
        except Exception:
            self.control_epoch += 1
            self._last_observation_id = None
            return {"outcome": "unknown", "action_id": action_id, **self.status_base(), "error": "ACTION_OUTCOME_UNKNOWN"}
        finally:
            self._action_in_progress = False
        if self.control_epoch != start_epoch:
            raise WorkerError("STALE_CONTROL_EPOCH", "control changed while the manual action was in progress")
        self.control_epoch += 1
        self._last_observation_id = None
        return {"outcome": "applied", "action_id": action_id, "result": result, **self.status_base()}

    @staticmethod
    def _normalize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(cookie, dict):
            raise WorkerError("INVALID_COOKIE", "each cookie must be an object", status_code=422)
        allowed = {"name", "value", "domain", "path", "expires", "expirationDate", "expires_at",
                   "httpOnly", "http_only", "secure", "sameSite", "same_site", "hostOnly", "host_only",
                   "session", "storeId", "store_id", "source_key", "site_ids", "unsupported_reason", "partition_key"}
        if set(cookie) - allowed:
            raise WorkerError("INVALID_COOKIE", "cookie contains unsupported fields", status_code=422)
        unsupported_reason = cookie.get("unsupported_reason")
        if unsupported_reason not in (None, ""):
            raise WorkerError("INVALID_COOKIE", "cookie is marked unsupported by its source", status_code=422)
        if cookie.get("partition_key") not in (None, ""):
            raise WorkerError("INVALID_COOKIE", "partitioned cookies are not supported by this browser profile", status_code=422)
        if "source_key" in cookie and (not isinstance(cookie["source_key"], str) or not cookie["source_key"] or len(cookie["source_key"]) > 4096):
            raise WorkerError("INVALID_COOKIE", "source_key is invalid", status_code=422)
        if "store_id" in cookie and not isinstance(cookie["store_id"], str):
            raise WorkerError("INVALID_COOKIE", "store_id is invalid", status_code=422)
        if "site_ids" in cookie and (not isinstance(cookie["site_ids"], list) or any(not isinstance(item, str) for item in cookie["site_ids"])):
            raise WorkerError("INVALID_COOKIE", "site_ids is invalid", status_code=422)
        name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
        if not isinstance(name, str) or not name or len(name) > 4096 or not isinstance(value, str) or len(value) > 16_384:
            raise WorkerError("INVALID_COOKIE", "cookie name/value is invalid", status_code=422)
        if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name, flags=re.ASCII):
            raise WorkerError("INVALID_COOKIE", "cookie name must be an ASCII HTTP token", status_code=422)
        if not isinstance(domain, str) or not domain or len(domain) > 253 or any(ord(ch) < 33 for ch in domain):
            raise WorkerError("INVALID_COOKIE", "cookie domain is invalid", status_code=422)
        try:
            bare_domain = domain.lstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise WorkerError("INVALID_COOKIE", "cookie domain is invalid", status_code=422) from exc
        if not bare_domain or any(not _DOMAIN_LABEL.fullmatch(label) for label in bare_domain.split(".")):
            raise WorkerError("INVALID_COOKIE", "cookie domain is invalid", status_code=422)
        path = cookie.get("path", "/")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 4096:
            raise WorkerError("INVALID_COOKIE", "cookie path must start with /", status_code=422)
        host_only = cookie.get("hostOnly", cookie.get("host_only"))
        if host_only is not None and not isinstance(host_only, bool):
            raise WorkerError("INVALID_COOKIE", "hostOnly must be boolean", status_code=422)
        cookie_domain = (f".{bare_domain}" if host_only is False or (host_only is None and domain.startswith("."))
                         else bare_domain)
        item: dict[str, Any] = {"name": name, "value": value, "domain": cookie_domain, "path": path}
        for field, alias in (("secure", "secure"), ("httpOnly", "http_only")):
            val = cookie.get(field, cookie.get(alias, False))
            if not isinstance(val, bool):
                raise WorkerError("INVALID_COOKIE", f"{field} must be boolean", status_code=422)
            item[field] = val
        expiration = cookie.get("expires", cookie.get("expirationDate", cookie.get("expires_at")))
        if expiration is not None and not cookie.get("session", False):
            if isinstance(expiration, bool) or not isinstance(expiration, (int, float)) or not math_isfinite(expiration):
                raise WorkerError("INVALID_COOKIE", "cookie expiry must be finite", status_code=422)
            if expiration > 0:
                item["expires"] = float(expiration)
        same_site = cookie.get("sameSite", cookie.get("same_site"))
        if same_site is not None:
            mapping = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None", "unspecified": None}
            if not isinstance(same_site, str) or same_site.lower() not in mapping:
                raise WorkerError("INVALID_COOKIE", "sameSite is invalid", status_code=422)
            if mapping[same_site.lower()] is not None:
                item["sameSite"] = mapping[same_site.lower()]
        if item.get("sameSite") == "None" and not item["secure"]:
            raise WorkerError("INVALID_COOKIE", "SameSite=None cookies must be Secure", status_code=422)
        if name.startswith("__Secure-") and not item["secure"]:
            raise WorkerError("INVALID_COOKIE", "__Secure- cookies must be Secure", status_code=422)
        if name.startswith("__Host-") and (not item["secure"] or path != "/" or cookie_domain.startswith(".")):
            raise WorkerError("INVALID_COOKIE", "__Host- cookies require Secure, path / and a host-only domain", status_code=422)
        return item

    async def import_cookies(self, body: CookieImportBody) -> dict[str, Any]:
        if self._import_in_progress or self._action_in_progress:
            raise WorkerError("CONTROL_BUSY", "another browser operation is in progress")
        self._import_in_progress = True
        try:
            return await self._import_cookies(body)
        finally:
            self._import_in_progress = False
            self.control_epoch += 1
            self._last_observation_id = None

    async def _import_cookies(self, body: CookieImportBody) -> dict[str, Any]:
        if self.control_owner != "none":
            raise WorkerError("CONTROL_BUSY", "cookie import is allowed only while input is unowned")
        if not self._started or self._adapter is None:
            raise WorkerError("BROWSER_UNAVAILABLE", "browser worker is not ready", status_code=503)
        # Validate the complete request before issuing any browser mutation.
        cookies = [self._normalize_cookie(cookie) for cookie in body.cookies]
        fingerprint = hashlib.sha256(_json(cookies).encode()).hexdigest()
        assert self._db is not None
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute("SELECT fingerprint,state,result_json FROM cookie_imports WHERE job_id=?", (body.job_id,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise WorkerError("COOKIE_IMPORT_CONFLICT", "job_id was reused with different cookies")
                self._db.execute("COMMIT")
                if row[1] == "complete" and row[2]:
                    return json.loads(row[2])
                return {"job_id": body.job_id, "outcome": "unknown", "error": "INTERRUPTED_AFTER_START"}
            now = _utc_now()
            self._db.execute("INSERT INTO cookie_imports(job_id,fingerprint,state,created_at,updated_at) VALUES(?,?, 'started',?,?)",
                             (body.job_id, fingerprint, now, now))
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        results: list[dict[str, Any]] = []
        import_epoch = self.control_epoch
        for index, cookie in enumerate(cookies):
            if self.control_owner != "none" or self.control_epoch != import_epoch:
                results.extend({"index": rest, "name": cookies[rest]["name"], "domain": cookies[rest]["domain"], "outcome": "not_attempted"}
                               for rest in range(index, len(cookies)))
                break
            from urllib.parse import quote
            row = {"index": index, "name": cookie["name"], "domain": cookie["domain"]}
            try:
                await asyncio.wait_for(self._adapter.set_cookies([cookie]), timeout=5.0)
                if self.control_owner != "none" or self.control_epoch != import_epoch:
                    raise RuntimeError("control changed during cookie import")
                url = "https://" + cookie["domain"].lstrip(".") + quote(cookie["path"], safe="/")
                observed = await asyncio.wait_for(self._adapter.get_cookies([url]), timeout=5.0)
                if self.control_owner != "none" or self.control_epoch != import_epoch:
                    raise RuntimeError("control changed during cookie verification")
                if not isinstance(observed, list) or len(observed) > 5000:
                    raise RuntimeError("invalid cookie verification response")
                matches = [item for item in observed if isinstance(item, dict) and
                           item.get("name") == cookie["name"] and item.get("domain") == cookie["domain"] and
                           item.get("path") == cookie["path"]]
                verified = len(matches) == 1 and all(matches[0].get(key) == cookie[key] for key in ("value", "secure", "httpOnly"))
                if verified and "sameSite" in cookie:
                    verified = matches[0].get("sameSite") == cookie["sameSite"]
                if verified:
                    actual_expiry = matches[0].get("expires")
                    verified = (isinstance(actual_expiry, (int, float)) and not isinstance(actual_expiry, bool) and
                                math.isfinite(actual_expiry) and
                                (abs(actual_expiry - cookie["expires"]) <= 1 if "expires" in cookie else actual_expiry == -1))
                results.append({**row, "outcome": "applied" if verified else "failed",
                                **({} if verified else {"error": "COOKIE_NOT_VERIFIED"})})
            except Exception:
                results.append({**row, "outcome": "unknown", "error": "COOKIE_OUTCOME_UNKNOWN"})
                results.extend({"index": rest, "name": cookies[rest]["name"], "domain": cookies[rest]["domain"], "outcome": "not_attempted"}
                               for rest in range(index + 1, len(cookies)))
                break
        result = {"job_id": body.job_id, "total": len(cookies), "applied": sum(item["outcome"] == "applied" for item in results),
                  "failed": sum(item["outcome"] != "applied" for item in results), "cookies": results}
        if any(item["outcome"] == "unknown" for item in results):
            result["outcome"] = "unknown"
        self._db.execute("UPDATE cookie_imports SET state='complete',result_json=?,updated_at=? WHERE job_id=?",
                         (_json(result), _utc_now(), body.job_id))
        return result


def math_isfinite(value: int | float) -> bool:
    import math
    return math.isfinite(value)


def _load_token() -> str | None:
    key_file = os.environ.get("WORKER_API_KEY_FILE")
    if key_file:
        try:
            value = Path(key_file).read_text(encoding="utf-8").strip()
            return value or None
        except OSError as exc:
            raise RuntimeError("WORKER_API_KEY_FILE could not be read") from exc
    return os.environ.get("WORKER_API_KEY") or None


def create_app(runtime: BrowserRuntime | Any | None = None, token: str | None = None) -> FastAPI:
    injected = runtime is not None and token is not None
    runtime = runtime or BrowserRuntime.from_env()
    auth_token = token

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal auth_token
        watchdog: asyncio.Task[Any] | None = None
        if auth_token is None:
            auth_token = _load_token()
        if not auth_token:
            raise RuntimeError("WORKER_API_KEY_FILE or WORKER_API_KEY is required")
        if hasattr(runtime, "start"):
            try:
                await runtime.start()
            except Exception:
                # A failed browser launch is visible via /health; it must not
                # turn a missing GPU/package into a falsely ready service.
                pass
        if hasattr(runtime, "recovery_watchdog"):
            watchdog = asyncio.create_task(runtime.recovery_watchdog())
        try:
            yield
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            if hasattr(runtime, "close"):
                await runtime.close()

    app = FastAPI(title="Browser worker", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.test_injected = injected

    async def require_bearer(authorization: str | None = Header(default=None)) -> None:
        expected = auth_token
        if expected is None:
            expected = _load_token()
        if not expected or not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "bearer authorization required"})
        provided = authorization[7:]
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "invalid bearer authorization"})

    def readiness_trace_allowed() -> None:
        if not ReadinessActionTrace.enabled():
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})

    @app.get("/diagnostics/readiness-actions", dependencies=[Depends(require_bearer)])
    async def readiness_actions() -> dict[str, Any]:
        readiness_trace_allowed()
        if not getattr(runtime, "_started", False) or getattr(runtime, "_adapter", None) is None:
            raise HTTPException(status_code=503, detail={"code": "BROWSER_UNAVAILABLE"})
        try:
            parsed = urlsplit(await runtime._adapter.url())
            path = parsed.path.rstrip("/") or "/"
            permitted = parsed.scheme == "https" and ((parsed.hostname == "demoqa.com" and path in {
                "/checkbox", "/webtables", "/text-box", "/buttons", "/dynamic-properties"}) or
                (parsed.hostname == "readiness-fixture.test" and path in {
                "/tree", "/form", "/overlay", "/checkbox", "/webtables", "/text-box", "/buttons", "/dynamic-properties"}))
            if not permitted:
                raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
            return {"records": runtime._readiness_trace.snapshot()}
        except HTTPException: raise
        except Exception as exc: raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc

    @app.post("/diagnostics/readiness-actions/reset", dependencies=[Depends(require_bearer)])
    async def reset_readiness_actions() -> dict[str, Any]:
        readiness_trace_allowed()
        if not getattr(runtime, "_started", False) or getattr(runtime, "_adapter", None) is None:
            raise HTTPException(status_code=503, detail={"code": "BROWSER_UNAVAILABLE"})
        try:
            parsed = urlsplit(await runtime._adapter.url())
            if parsed.scheme != "https" or parsed.hostname not in {"demoqa.com", "readiness-fixture.test"} or parsed.path.rstrip("/") not in {
                    "/tree", "/form", "/overlay", "/checkbox", "/webtables", "/text-box", "/buttons", "/dynamic-properties"}:
                raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
        except HTTPException: raise
        except Exception as exc: raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc
        runtime._readiness_trace.reset()
        return {"reset": True}

    @app.post("/diagnostics/readiness-fixture", dependencies=[Depends(require_bearer)])
    async def install_readiness_fixture(body: dict[str, Any]) -> dict[str, Any]:
        readiness_trace_allowed()
        if (set(body) != {"scenario"} or not isinstance(body.get("scenario"), str)
                or body["scenario"] not in {"tree", "form", "overlay"}):
            raise HTTPException(status_code=422, detail={"code": "INVALID_FIXTURE_SCENARIO"})
        if not getattr(runtime, "_started", False) or getattr(runtime, "_page", None) is None:
            raise HTTPException(status_code=503, detail={"code": "BROWSER_UNAVAILABLE"})
        if (getattr(runtime, "_action_in_progress", False) or getattr(runtime, "_import_in_progress", False)
                or getattr(runtime, "control_owner", "none") != "none"):
            raise HTTPException(status_code=409, detail={"code": "CONTROL_BUSY"})
        context = getattr(runtime, "_context", None)
        if context is not None:
            try:
                pages = list(context.pages)
                if len(pages) != 1 or pages[0] is not runtime._page:
                    raise HTTPException(status_code=409, detail={"code": "PAGE_CONTEXT_UNSAFE"})
            except HTTPException: raise
            except Exception as exc: raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc
        scenario = body["scenario"]
        fixture_url = f"https://readiness-fixture.test/{scenario}"
        try:
            from scripts.readiness_p0_fixture import fixture_html
            html = fixture_html(scenario)
            if not isinstance(html, str) or len(html) > 100_000:
                raise ValueError("invalid static fixture")
            async def fulfill(route: Any) -> None:
                request_url = route.request.url
                if request_url != fixture_url:
                    await route.abort()
                    return
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            await runtime._page.route(fixture_url, fulfill)
            return {"installed": True, "scenario": scenario, "path": f"/{scenario}"}
        except HTTPException: raise
        except Exception as exc: raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc

    def as_http(exc: WorkerError) -> HTTPException:
        return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})

    @app.get("/health", dependencies=[Depends(require_bearer)])
    async def health() -> dict[str, Any]:
        return await runtime.health()

    @app.get("/status", dependencies=[Depends(require_bearer)])
    async def status() -> dict[str, Any]:
        return await runtime.status()

    @app.post("/recover", dependencies=[Depends(require_bearer)])
    async def recover(_body: RecoverBody) -> dict[str, Any]:
        try:
            return await runtime.recover()
        except WorkerError as exc:
            raise as_http(exc)

    @app.get("/diagnostics/demoqa-inputs", dependencies=[Depends(require_bearer)])
    async def demoqa_input_diagnostics() -> dict[str, Any]:
        # This narrow, fixed probe is available only on explicitly enabled
        # test/debug workers. It never returns input values, page text, or
        # caller-supplied JavaScript/selectors.
        if (os.environ.get("APP_ENV", "production").lower() not in {"test", "debug"}
                or os.environ.get("DEMOQA_DIAGNOSTICS") != "1"):
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
        if not runtime._started or runtime._page is None or runtime._adapter is None:
            raise HTTPException(status_code=503, detail={"code": "BROWSER_UNAVAILABLE"})
        try:
            if _origin(await runtime._adapter.url()) != "https://demoqa.com":
                raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
            data = await asyncio.wait_for(runtime._page.evaluate(DEMOQA_INPUT_DIAGNOSTICS), timeout=2)
            if not isinstance(data, dict) or set(data) != {
                    "input_count", "truncated", "selector_counts", "inputs",
                    "role_checkbox_count", "role_checkboxes_truncated", "role_checkboxes"}:
                raise ValueError("invalid diagnostic shape")
            if (type(data["input_count"]) is not int or not 0 <= data["input_count"] <= 64
                    or type(data["truncated"]) is not bool or not isinstance(data["inputs"], list)
                    or len(data["inputs"]) > 64 or data["input_count"] != len(data["inputs"])):
                raise ValueError("invalid diagnostic bounds")
            count_fields = {"checkbox_inputs", "desktop_id", "desktop_labels", "aria_checkboxes"}
            if (not isinstance(data["selector_counts"], dict) or set(data["selector_counts"]) != count_fields
                    or any(type(value) is not int or not 0 <= value <= 64 for value in data["selector_counts"].values())):
                raise ValueError("invalid selector counts")
            role_fields = {"tag", "id", "role", "aria_label", "aria_checked", "checked", "visible",
                           "class_names", "accessible_label", "static_label", "selector"}
            if (type(data["role_checkbox_count"]) is not int or not 0 <= data["role_checkbox_count"] <= 64
                    or type(data["role_checkboxes_truncated"]) is not bool
                    or not isinstance(data["role_checkboxes"], list) or len(data["role_checkboxes"]) > 64
                    or data["role_checkbox_count"] != len(data["role_checkboxes"])):
                raise ValueError("invalid role checkbox bounds")
            role_checkboxes: list[dict[str, Any]] = []
            for item in data["role_checkboxes"]:
                if not isinstance(item, dict) or set(item) != role_fields:
                    raise ValueError("invalid role checkbox metadata")
                if (any(not isinstance(item[key], str) or len(item[key]) > 64
                        for key in ("tag", "id", "role", "aria_label"))
                        or (item["aria_checked"] is not None and
                            (not isinstance(item["aria_checked"], str) or len(item["aria_checked"]) > 64))
                        or (item["checked"] is not None and type(item["checked"]) is not bool)
                        or type(item["visible"]) is not bool
                        or any(not isinstance(item[key], str) or len(item[key]) > 128
                               for key in ("accessible_label", "static_label"))
                        or (item["selector"] is not None and
                            (not isinstance(item["selector"], str) or len(item["selector"]) > 256))
                        or not isinstance(item["class_names"], list) or len(item["class_names"]) > 8
                        or any(not isinstance(name, str) or len(name) > 64 for name in item["class_names"])):
                    raise ValueError("invalid role checkbox metadata bounds")
                safe_item = {key: item[key] for key in role_fields}
                selector = item["selector"]
                if selector is not None:
                    try:
                        matches = await asyncio.wait_for(runtime._page.locator(selector).count(), timeout=2)
                        if matches != 1:
                            safe_item["selector"] = None
                    except Exception:
                        safe_item["selector"] = None
                role_checkboxes.append(safe_item)
            fields = {"tag", "type", "id", "name", "aria_label", "pattern", "checked",
                      "validity_valid", "aria_checked", "class_names", "labels"}
            sanitized: list[dict[str, Any]] = []
            for item in data["inputs"]:
                if not isinstance(item, dict) or set(item) != fields:
                    raise ValueError("invalid input metadata")
                if (any(not isinstance(item[key], str) or len(item[key]) > 64
                        for key in ("tag", "type", "id", "name", "aria_label"))
                        or not isinstance(item["pattern"], str) or len(item["pattern"]) > 128
                        or type(item["checked"]) is not bool or type(item["validity_valid"]) is not bool
                        or (item["aria_checked"] is not None and
                            (not isinstance(item["aria_checked"], str) or len(item["aria_checked"]) > 64))
                        or not isinstance(item["labels"], list) or len(item["labels"]) > 8
                        or any(not isinstance(label, str) or len(label) > 128 for label in item["labels"])
                        or not isinstance(item["class_names"], list) or len(item["class_names"]) > 8
                        or any(not isinstance(name, str) or len(name) > 64 for name in item["class_names"])):
                    raise ValueError("invalid input metadata bounds")
                sanitized.append({key: item[key] for key in fields})
            return {"input_count": data["input_count"], "truncated": data["truncated"],
                    "selector_counts": data["selector_counts"], "inputs": sanitized,
                    "role_checkbox_count": data["role_checkbox_count"],
                    "role_checkboxes_truncated": data["role_checkboxes_truncated"],
                    "role_checkboxes": role_checkboxes}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc

    @app.get("/diagnostics/demoqa-fonts", dependencies=[Depends(require_bearer)])
    async def demoqa_font_diagnostics() -> dict[str, Any]:
        # Fixed, read-only metadata for the public DemoQA heading and font set.
        if (os.environ.get("APP_ENV", "production").lower() not in {"test", "debug"}
                or os.environ.get("DEMOQA_DIAGNOSTICS") != "1"):
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
        if not runtime._started or runtime._page is None or runtime._adapter is None:
            raise HTTPException(status_code=503, detail={"code": "BROWSER_UNAVAILABLE"})
        try:
            if _origin(await runtime._adapter.url()) != "https://demoqa.com":
                raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
            data = await asyncio.wait_for(runtime._page.evaluate(DEMOQA_FONT_DIAGNOSTICS), timeout=2)
            if not isinstance(data, dict) or set(data) != {
                    "heading_selector", "heading_match_count", "header_text", "font_family", "fonts_status", "fonts_check",
                    "font_faces_count", "font_faces_truncated", "font_faces",
                    "font_resources_count", "font_resources_truncated", "font_resources"}:
                raise ValueError("invalid font diagnostic shape")
            if (data["heading_selector"] not in {"main-header", "main-h1", "h1", "none"}
                    or type(data["heading_match_count"]) is not int
                    or not 0 <= data["heading_match_count"] <= 64
                    or (data["heading_selector"] == "none") != (data["heading_match_count"] == 0)
                    or (data["heading_selector"] != "none" and data["heading_match_count"] != 1)
                    or any(not isinstance(data[key], str) or len(data[key]) > limit
                           for key, limit in (("header_text", 64), ("font_family", 128), ("fonts_status", 64)))
                    or type(data["fonts_check"]) is not bool):
                raise ValueError("invalid heading font metadata")
            for count_key, truncated_key, list_key, maximum in (
                    ("font_faces_count", "font_faces_truncated", "font_faces", 16),
                    ("font_resources_count", "font_resources_truncated", "font_resources", 16)):
                values = data[list_key]
                if (type(data[count_key]) is not int or not 0 <= data[count_key] <= maximum
                        or type(data[truncated_key]) is not bool or not isinstance(values, list)
                        or len(values) > maximum or data[count_key] != len(values)):
                    raise ValueError("invalid font diagnostic bounds")
            safe_faces: list[dict[str, str]] = []
            for face in data["font_faces"]:
                if (not isinstance(face, dict) or set(face) != {"family", "status"}
                        or any(not isinstance(face[key], str) or len(face[key]) > 64 for key in ("family", "status"))):
                    raise ValueError("invalid font face metadata")
                safe_faces.append({"family": face["family"], "status": face["status"]})
            safe_resources: list[dict[str, str]] = []
            for resource in data["font_resources"]:
                if (not isinstance(resource, dict) or set(resource) != {"origin", "path"}
                        or not isinstance(resource["origin"], str) or len(resource["origin"]) > 128
                        or not isinstance(resource["path"], str) or len(resource["path"]) > 128
                        or "?" in resource["path"] or "#" in resource["path"]
                        or not resource["path"].startswith("/")):
                    raise ValueError("invalid font resource metadata")
                parsed_origin = _origin(resource["origin"])
                if parsed_origin != resource["origin"]:
                    raise ValueError("invalid font resource origin")
                safe_resources.append({"origin": resource["origin"], "path": resource["path"]})
            return {
                "heading_selector": data["heading_selector"],
                "heading_match_count": data["heading_match_count"],
                "header_text": data["header_text"],
                "font_family": data["font_family"], "fonts_status": data["fonts_status"],
                "fonts_check": data["fonts_check"], "font_faces_count": data["font_faces_count"],
                "font_faces_truncated": data["font_faces_truncated"], "font_faces": safe_faces,
                "font_resources_count": data["font_resources_count"],
                "font_resources_truncated": data["font_resources_truncated"],
                "font_resources": safe_resources,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc

    @app.post("/diagnostics/demoqa-font-style", dependencies=[Depends(require_bearer)])
    async def demoqa_font_style_diagnostic(body: dict[str, Any]) -> dict[str, Any]:
        allowed_variants = {"baseline", "arial_normal", "arial_medium", "arial_thin100",
                            "system_normal", "system_bold", "system_thin100", "baseline_normal", "monospace"}
        if set(body) != {"variant"} or not isinstance(body.get("variant"), str) or body["variant"] not in allowed_variants:
            raise HTTPException(status_code=422, detail={"code": "INVALID_DIAGNOSTIC_VARIANT"})
        if (os.environ.get("APP_ENV", "production").lower() not in {"test", "debug"}
                or os.environ.get("DEMOQA_DIAGNOSTICS") != "1"):
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
        if not runtime._started or runtime._page is None or runtime._adapter is None:
            raise HTTPException(status_code=503, detail={"code": "BROWSER_UNAVAILABLE"})
        try:
            if _origin(await runtime._adapter.url()) != "https://demoqa.com":
                raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
            data = await asyncio.wait_for(
                runtime._page.evaluate(DEMOQA_FONT_STYLE_DIAGNOSTIC, {"variant": body["variant"]}), timeout=2)
            expected_fields = {"variant", "heading_selector", "heading_text", "font_family", "font_weight"}
            if not isinstance(data, dict) or set(data) != expected_fields:
                raise ValueError("invalid style diagnostic shape")
            if (data["variant"] != body["variant"]
                    or data["heading_selector"] not in {"main-header", "main-h1", "h1"}
                    or any(not isinstance(data[key], str) or len(data[key]) > limit for key, limit in (
                        ("heading_text", 64), ("font_family", 128), ("font_weight", 16)))):
                raise ValueError("invalid style diagnostic fields")
            return {key: data[key] for key in expected_fields}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc

    @app.get("/capabilities/gemini-prompt-submit/diagnostics", dependencies=[Depends(require_bearer)])
    async def gemini_capability_diagnostics() -> dict[str, Any]:
        from worker.site_capabilities import GEMINI_DIAGNOSTICS, GEMINI_ORIGIN
        if not runtime._started or runtime._page is None or runtime._adapter is None:
            raise HTTPException(status_code=503, detail={"code": "BROWSER_UNAVAILABLE"})
        try:
            if _origin(await runtime._adapter.url()) != GEMINI_ORIGIN:
                raise HTTPException(status_code=409, detail={"code": "POLICY_DENIED"})
            data = await asyncio.wait_for(runtime._page.evaluate(GEMINI_DIAGNOSTICS), timeout=2)
            fields = {"gemini_main_frame", "input_area_count", "input_container_count", "composer_count",
                      "send_button_count", "active_composer", "dialog_count", "known_send_label"}
            if not isinstance(data, dict) or set(data) != fields:
                raise ValueError("invalid diagnostic data")
            if any(type(data[k]) is not bool for k in ("gemini_main_frame", "active_composer", "known_send_label")):
                raise ValueError("invalid diagnostic boolean")
            if any(type(data[k]) is not int or not 0 <= data[k] <= 99 for k in fields - {"gemini_main_frame", "active_composer", "known_send_label"}):
                raise ValueError("invalid diagnostic count")
            return data
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "CAPABILITY_UNAVAILABLE"}) from exc

    @app.get("/capabilities/gemini-auth/status", dependencies=[Depends(require_bearer)])
    async def gemini_auth_status() -> dict[str, str]:
        return await runtime.gemini_auth_status()

    @app.post("/gate", dependencies=[Depends(require_bearer)])
    async def gate(body: GateBody) -> dict[str, Any]:
        try:
            return runtime.gate(body.owner)
        except WorkerError as exc:
            raise as_http(exc)

    @app.post("/heartbeat", dependencies=[Depends(require_bearer)])
    async def heartbeat(body: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(body) != {"control_epoch"}:
                raise WorkerError("INVALID_REQUEST", "heartbeat accepts only control_epoch", status_code=422)
            epoch = body.get("control_epoch")
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
                raise WorkerError("INVALID_REQUEST", "control_epoch must be a nonnegative integer", status_code=422)
            return runtime.heartbeat(epoch)
        except WorkerError as exc:
            raise as_http(exc)

    @app.post("/observe", dependencies=[Depends(require_bearer)])
    async def observe() -> dict[str, Any]:
        try:
            return await runtime.observe()
        except WorkerError as exc:
            raise as_http(exc)

    @app.post("/view/reset", dependencies=[Depends(require_bearer)])
    async def reset_view(body: EpochBody) -> dict[str, Any]:
        try:
            return await runtime.reset_view(body.control_epoch)
        except WorkerError as exc:
            raise as_http(exc)

    @app.post("/verify", dependencies=[Depends(require_bearer)])
    async def verify(body: dict[str, Any]) -> dict[str, Any]:
        try:
            if set(body) != {"conditions"}:
                raise WorkerError("INVALID_CONDITION", "verify accepts only conditions", status_code=422)
            conditions = body.get("conditions")
            if not isinstance(conditions, list):
                raise WorkerError("INVALID_CONDITION", "conditions must be a list", status_code=422)
            return await runtime.verify(conditions)
        except WorkerError as exc:
            raise as_http(exc)

    @app.get("/screenshot", dependencies=[Depends(require_bearer)])
    async def screenshot() -> Response:
        try:
            return Response(content=await runtime.screenshot(), media_type="image/png", headers={"Cache-Control": "no-store"})
        except WorkerError as exc:
            raise as_http(exc)

    @app.post("/execute", dependencies=[Depends(require_bearer)])
    async def execute(body: AgentExecuteBody) -> dict[str, Any]:
        try:
            return await runtime.execute_agent(body)
        except WorkerError as exc:
            raise as_http(exc)

    @app.post("/manual/execute", dependencies=[Depends(require_bearer)])
    async def manual_execute(body: ManualExecuteBody) -> dict[str, Any]:
        try:
            return await runtime.execute_manual(body)
        except WorkerError as exc:
            raise as_http(exc)

    @app.post("/cookies/import", dependencies=[Depends(require_bearer)])
    async def import_cookies(body: CookieImportBody) -> dict[str, Any]:
        try:
            return await runtime.import_cookies(body)
        except WorkerError as exc:
            raise as_http(exc)

    return app


app = create_app()


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the local browser worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7070)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, limit_concurrency=16, backlog=32, access_log=False)


if __name__ == "__main__":
    main()

