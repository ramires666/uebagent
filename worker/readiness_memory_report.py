"""Fixed, privacy-filtered about:memory DOM collector for readiness tests."""
from __future__ import annotations

from typing import Any


CANONICAL_FIXTURE_URL = "https://readiness-fixture.test/tree"
ABOUT_MEMORY_URL = "about:memory"
REPORT_MAIN_SECONDS = 15.0
REPORT_RESTORE_SECONDS = 5.0
REPORT_TOTAL_SECONDS = REPORT_MAIN_SECONDS + REPORT_RESTORE_SECONDS
REPORT_METRICS = frozenset({
    "explicit", "window_objects", "js_non_window", "images", "heap_unclassified",
    "heap_overhead", "resident", "heap_allocated",
})
PROCESS_KINDS = frozenset({"main", "web_content", "web_extensions", "other"})
NATIVE_METRIC_NAMES = {
    "explicit": "explicit", "window-objects": "window_objects",
    "js-non-window": "js_non_window", "images": "images",
    "heap-unclassified": "heap_unclassified", "heap-overhead": "heap_overhead",
    "resident": "resident", "heap-allocated": "heap_allocated",
}
MAX_METRIC_BYTES = 1_099_511_627_776  # 1 TiB

# Pinned Camoufox 0.4.11 aboutMemory.js source: #verbose and #measureButton;
# the anonymize input is nested in a label whose text is exactly "anonymize".
# Verbose formatBytes is en-US integer bytes ("1,234 B"). This script only
# returns process roles, fixed metric keys, and safe integer byte counts.
MEMORY_REPORT_READY = """() => {
  if (location.href !== 'about:memory' || document.title !== 'about:memory (live measurement)') return false;
  const names = [...document.querySelectorAll('h1')].map(h => h.textContent.trim());
  // Do not require a Web Content heading: navigating this only page to
  // about:memory can tear down the original renderer before reporters run.
  return names.length > 0 && document.querySelectorAll('.mrValue').length > 0;
}"""

MEMORY_REPORT_DOM_SNAPSHOT = r"""() => {
  if (location.href !== 'about:memory') return null;
  const headings = [...document.querySelectorAll('h1')];
  const explicitKeys = new Set([
    'explicit', 'window-objects', 'js-non-window', 'images',
    'heap-unclassified', 'heap-overhead'
  ]);
  const otherKeys = new Set(['resident', 'heap-allocated']);
  const roleFor = text => {
    if (/^Main Process(?:\s|$)/.test(text)) return 'main';
    if (/^Web Content(?:\s|$)/.test(text)) return 'web_content';
    if (/^WebExtensions(?:\s|$)/.test(text)) return 'web_extensions';
    return 'other';
  };
  const parseBytes = text => {
    const match = /^([0-9][0-9,]*) B$/.exec(text.trim());
    if (!match) return null;
    const value = Number(match[1].replaceAll(',', ''));
    return Number.isSafeInteger(value) && value >= 0 ? value : null;
  };
  const processes = [];
  for (const heading of headings) {
    const kind = roleFor(heading.textContent.trim());
    const metrics = {};
    const ambiguous = new Set();
    let section = '';
    for (let node = heading.nextElementSibling; node && node.tagName !== 'H1'; node = node.nextElementSibling) {
      if (node.tagName === 'H2') section = node.textContent.trim();
      if (!node.matches('pre.entries')) continue;
      const allowed = section === 'Explicit Allocations' ? explicitKeys :
        section === 'Other Measurements' ? otherKeys : null;
      if (!allowed) continue;
      for (const item of node.querySelectorAll('span[role="listitem"]')) {
        const name = item.querySelector('.mrName')?.textContent.trim();
        if (!allowed.has(name)) continue;
        const valueText = item.querySelector('.mrValue')?.textContent.trim() ?? '';
        const value = parseBytes(valueText);
        if (Object.hasOwn(metrics, name)) ambiguous.add(name);
        else if (value !== null) metrics[name] = value;
      }
    }
    for (const key of ambiguous) delete metrics[key];
    if (Object.keys(metrics).length) processes.push({ kind, metrics });
  }
  return { processes };
}"""


def normalize_dom_snapshot(payload: Any) -> list[dict[str, Any]]:
    """Accept only the report script's strict safe shape and numeric metrics."""
    if not isinstance(payload, dict) or set(payload) != {"processes"}:
        raise ValueError("report_shape_invalid")
    processes = payload["processes"]
    if not isinstance(processes, list) or not 1 <= len(processes) <= 64:
        raise ValueError("report_processes_invalid")
    result = []
    for process in processes:
        if not isinstance(process, dict) or set(process) != {"kind", "metrics"}:
            raise ValueError("report_process_invalid")
        kind, metrics = process["kind"], process["metrics"]
        if kind not in PROCESS_KINDS or not isinstance(metrics, dict) or not metrics:
            raise ValueError("report_metrics_invalid")
        if set(metrics) - NATIVE_METRIC_NAMES.keys():
            raise ValueError("report_metric_unknown")
        if any(type(value) is not int or not 0 <= value <= MAX_METRIC_BYTES
               for value in metrics.values()):
            raise ValueError("report_value_invalid")
        normalized = {NATIVE_METRIC_NAMES[key]: value for key, value in metrics.items()}
        if set(normalized) - REPORT_METRICS:
            raise ValueError("report_metric_unknown")
        result.append({"kind": kind, "metrics": normalized})
    return result
