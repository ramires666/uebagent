"""Bounded, read-only hints aligned with a browser observation screenshot."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import Any


MAX_ELEMENTS = 32
MAX_SERIALIZED_BYTES = 4096
LABEL_MAX = 80
PROBE_TIMEOUT_SECONDS = 1.0

INTERACTION_HINTS_JS = r"""({width,height}) => {
  const vw=Number(width), vh=Number(height);
  if(location.protocol!=='https:'||!Number.isFinite(vw)||!Number.isFinite(vh)||vw<=0||vh<=0||vw!==innerWidth||vh!==innerHeight)return {version:1,coordinate_space:'normalized_1000',elements:[]};
  const selector='button,input,textarea,select,a[href],[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="textbox"],[role="combobox"],[role="treeitem"],[role="option"],[role="switch"],[role="slider"],[aria-expanded],.rc-tree-switcher,.rc-tree-checkbox,.rct-collapse,.rct-collapse-btn,.rct-checkbox,[tabindex]:not([tabindex="-1"])';
  const allowedRoles=new Set(['button','link','checkbox','radio','textbox','combobox','treeitem','option','switch','slider','tab','menuitem']);
  const cleanLabel=s=>String(s||'').replace(/\s+/g,' ').trim().slice(0,80);
  const rowLabel=e=>{
    const row=e.closest('[role="treeitem"],.rc-tree-treenode,.rct-node'); if(!row)return '';
    const aria=row.getAttribute('aria-label')||row.querySelector('[role="checkbox"][aria-label],.rc-tree-checkbox[aria-label],.rct-checkbox[aria-label]')?.getAttribute('aria-label');
    if(aria)return cleanLabel(aria);
    const title=row.querySelector('.rc-tree-title,.rct-title'); return cleanLabel(title?.textContent||'');
  };
  const labelOf=e=>{
    let label=e.getAttribute('aria-label')||'';
    if(!label&&e.labels?.length)label=Array.from(e.labels).map(n=>n.textContent||'').join(' ');
    if(!label&&e.tagName.toLowerCase()==='button')label=e.innerText||e.textContent||'';
    if(!label&&e.matches('[role="button"]'))label=e.innerText||e.textContent||'';
    if(!label&&e.hasAttribute('placeholder'))label=e.getAttribute('placeholder')||'';
    if(!label&&e.matches('.rc-tree-checkbox,.rct-checkbox'))label=rowLabel(e);
    if(!label&&e.matches('.rc-tree-switcher,.rct-collapse,.rct-collapse-btn,[aria-expanded]'))label=rowLabel(e);
    return cleanLabel(label);
  };
  const roleOf=e=>{
    const role=(e.getAttribute('role')||'').toLowerCase(); if(allowedRoles.has(role))return role;
    const tag=e.tagName.toLowerCase(), type=(e.getAttribute('type')||'').toLowerCase();
    if(e.matches('.rc-tree-checkbox,.rct-checkbox'))return 'checkbox';
    if(tag==='button'||tag==='select')return 'button';
    if(tag==='a')return 'link';
    if(tag==='input'&&type==='checkbox')return 'checkbox';
    if(tag==='input'&&type==='radio')return 'radio';
    if(tag==='input'||tag==='textarea')return 'textbox';
    if(e.matches('.rc-tree-switcher,.rct-collapse,.rct-collapse-btn,[aria-expanded]'))return 'button';
    return 'other';
  };
  const expandedOf=e=>{
    const v=e.getAttribute('aria-expanded'); if(v==='true')return true;if(v==='false')return false;
    const cls=String(e.className||'');
    if(/(?:^|\s)(?:rc-tree-switcher_open|rct-node-expanded)(?:\s|$)/.test(cls))return true;
    if(/(?:^|\s)(?:rc-tree-switcher_close|rct-node-collapsed)(?:\s|$)/.test(cls))return false;
    return undefined;
  };
  const out=[]; const nodes=document.querySelectorAll(selector);
  for(const e of nodes){
    if(out.length>=32)break;
    if(e.matches('input[type="password"]')||e.disabled===true||e.getAttribute('aria-disabled')==='true')continue;
    const style=getComputedStyle(e), r=e.getBoundingClientRect();
    if(style.display==='none'||style.visibility==='hidden'||Number(style.opacity)===0||!r.width||!r.height||!e.getClientRects().length)continue;
    const left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(vw,r.right),bottom=Math.min(vh,r.bottom);
    if(right<=left||bottom<=top)continue;
    const cx=(left+right)/2,cy=(top+bottom)/2,hit=document.elementFromPoint(cx,cy);
    const hitControl=hit?.closest(selector);
    if(hitControl&&hitControl!==e&&e.contains(hitControl))continue;
    if(!hit||!(e===hit||e.contains(hit)))continue;
    const role=roleOf(e), tag=e.tagName.toLowerCase(), label=labelOf(e);
    const norm=v=>Math.min(999.99,Math.max(0,Math.round(v*100)/100));
    const bx=norm(left/vw*1000),by=norm(top/vh*1000),br=Math.min(1000,Math.round(right/vw*100000)/100),bb=Math.min(1000,Math.round(bottom/vh*100000)/100);
    const bw=Math.round((br-bx)*100)/100,bh=Math.round((bb-by)*100)/100;
    if(bw<=0||bh<=0)continue;
    const center={x:norm(cx/vw*1000),y:norm(cy/vh*1000)};
    center.x=Math.min(center.x,bx+bw);center.y=Math.min(center.y,by+bh);
    if(center.x>=1000||center.y>=1000||center.x<bx||center.y<by)continue;
    const item={tag,role,label,center,
      bounds:{x:bx,y:by,width:bw,height:bh}};
    const check=e.getAttribute('aria-checked');
    if(role==='checkbox'||role==='radio'){
      if(check==='true'||check==='false')item.checked=check==='true';
      else if(typeof e.checked==='boolean')item.checked=e.checked;
      else if(e.classList.contains('rc-tree-checkbox-checked'))item.checked=true;
      else if(e.classList.contains('rc-tree-checkbox-unchecked'))item.checked=false;
      else if(e.classList.contains('rct-checkbox-check'))item.checked=true;
    }
    let expanded=expandedOf(e);
    if(typeof expanded!=='boolean'){
      const row=e.closest('[role="treeitem"],.rc-tree-treenode,.rct-node');
      if(row){const rv=row.getAttribute('aria-expanded');if(rv==='true')expanded=true;else if(rv==='false')expanded=false;else expanded=expandedOf(row);}
    }
    if(typeof expanded==='boolean')item.expanded=expanded;
    if(e.matches('.rc-tree-switcher,.rct-collapse,.rct-collapse-btn,[aria-expanded]'))item.kind='expander';
    else item.kind='control';
    if(tag==='input'||tag==='textarea')item.value_present=String(e.value||'').length>0;
    out.push(item);
  }
  return {version:1,coordinate_space:'normalized_1000',elements:out};
}"""


def enabled() -> bool:
    return os.environ.get("BROWSER_OBSERVATION_HINTS") == "1"


def empty_hints() -> dict[str, Any]:
    return {"version": 1, "coordinate_space": "normalized_1000", "elements": []}


def _box(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        return None
    if any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) for k in value):
        return None
    if any(value[k] < 0 or value[k] > 1000 for k in value):
        return None
    if value["width"] <= 0 or value["height"] <= 0 or value["x"] + value["width"] > 1000 or value["y"] + value["height"] > 1000:
        return None
    rounded = {key: round(float(value[key]), 2) for key in ("x", "y", "width", "height")}
    if (rounded["x"] + rounded["width"] > 1000
            or rounded["y"] + rounded["height"] > 1000
            or rounded["width"] <= 0 or rounded["height"] <= 0):
        return None
    return rounded


def _sanitize_element(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    roles = {"button", "link", "checkbox", "radio", "textbox", "combobox", "treeitem", "option", "switch", "slider", "tab", "menuitem", "other"}
    tag, role, label = raw.get("tag"), raw.get("role"), raw.get("label")
    if (type(tag) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", tag)
            or type(role) is not str or role not in roles or not isinstance(label, str)):
        return None
    label = " ".join(label.split())[:LABEL_MAX]
    center, bounds = raw.get("center"), _box(raw.get("bounds"))
    if not isinstance(center, dict) or set(center) != {"x", "y"} or bounds is None:
        return None
    if any(type(center[k]) not in (int, float) or not math.isfinite(center[k]) or not 0 <= center[k] < 1000 for k in center):
        return None
    safe_center = {}
    for axis in ("x", "y"):
        extent = "width" if axis == "x" else "height"
        low, high = bounds[axis], bounds[axis] + bounds[extent]
        if center[axis] < low - 0.01 or center[axis] > high + 0.01:
            return None
        safe_center[axis] = min(999.99, max(low, min(high, round(float(center[axis]), 2))))
    safe: dict[str, Any] = {"tag": tag, "role": role, "label": label,
        "center": safe_center, "bounds": bounds}
    for key in ("checked", "expanded", "value_present"):
        if key in raw and type(raw[key]) is bool:
            safe[key] = raw[key]
    if type(raw.get("kind")) is str and raw["kind"] in {"expander", "control"}:
        safe["kind"] = raw["kind"]
    return safe


def sanitize_hints(raw: Any) -> dict[str, Any]:
    if (not isinstance(raw, dict) or type(raw.get("version")) is not int or raw.get("version") != 1
            or raw.get("coordinate_space") != "normalized_1000"
            or not isinstance(raw.get("elements"), list)):
        return empty_hints()
    elements = []
    for item in raw["elements"][:MAX_ELEMENTS]:
        safe = _sanitize_element(item)
        if safe is not None:
            elements.append(safe)
    result = {"version": 1, "coordinate_space": "normalized_1000", "elements": elements}
    def encoded_size() -> int:
        return len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if encoded_size() > MAX_SERIALIZED_BYTES:
        for element in elements:
            element["label"] = element["label"][:64]
    while elements and encoded_size() > MAX_SERIALIZED_BYTES:
        elements.pop()
        result["elements"] = elements
    if encoded_size() > MAX_SERIALIZED_BYTES:
        return empty_hints()
    return result


async def collect_interaction_hints(page: Any, width: int, height: int) -> dict[str, Any]:
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return empty_hints()
    try:
        raw = await asyncio.wait_for(page.evaluate(INTERACTION_HINTS_JS, {"width": width, "height": height}),
                                     timeout=PROBE_TIMEOUT_SECONDS)
        return sanitize_hints(raw)
    except Exception:
        return empty_hints()
