"""Fixed, fail-closed DOM recognition for a single Gemini prompt submission."""

GEMINI_ORIGIN = "https://gemini.google.com"

GEMINI_AUTH_STATUS = r"""() => {
  const unknown = reason_code => ({status: 'unknown', reason_code});
  if (window !== window.top) return unknown('PROBE_UNAVAILABLE');
  if (location.origin !== 'https://gemini.google.com') return unknown('NOT_GEMINI');
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0
      && r.width > 0 && r.height > 0;
  };
  const roots = [...document.querySelectorAll('header,[role="banner"],main,[role="main"],[role="dialog"]')]
    .filter(visible);
  const inChrome = e => roots.some(root => root === e || root.contains(e))
    && !e.closest('rich-textarea,.ql-editor,[contenteditable="true"],user-query,model-response,[data-testid*="conversation"]');
  const signInLabels = new Set(['sign in', 'log in', 'войти', 'вход']);
  const signIn = [...document.querySelectorAll('a,button,[role="button"]')].some(e => {
    if (!visible(e) || !inChrome(e)) return false;
    const label = (e.getAttribute('aria-label') || e.getAttribute('title')
      || (e.children.length === 0 ? e.textContent : '') || '').trim().toLowerCase();
    if (signInLabels.has(label)) return true;
    if (e.tagName !== 'A' || !e.href) return false;
    try {
      const u = new URL(e.href);
      return u.origin === 'https://accounts.google.com'
        && /^\/(servicelogin|signin)(\/|$)/i.test(u.pathname);
    } catch (_) { return false; }
  });
  const accountPrefix = /^(google account|аккаунт google)\s*:/i;
  const emailInLabel = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i;
  const accountControls = [...document.querySelectorAll('button[aria-label],a[aria-label],[role="button"][aria-label]')]
    .filter(e => visible(e) && inChrome(e) && accountPrefix.test(e.getAttribute('aria-label') || ''));
  const accountProof = accountControls.length === 1
    && emailInLabel.test(accountControls[0].getAttribute('aria-label') || '')
    && [...accountControls[0].querySelectorAll('img')].some(visible);
  if (signIn && accountProof) return unknown('PROOF_INCOMPLETE');
  if (signIn) return {status: 'signed_out', reason_code: 'SIGN_IN_CONTROL_PRESENT'};
  if (accountProof) return {status: 'signed_in', reason_code: 'ACCOUNT_CONTROLS_PRESENT'};
  return unknown('PROOF_INCOMPLETE');
}"""

# Read only guard for the one automatic page refresh after cookie sync. It
# deliberately returns booleans/enums only; no draft text, path, or URL leaves
# the page. Unknown layouts fail closed.
GEMINI_SESSION_REFRESH_PREFLIGHT = r"""() => {
  const result = reason_code => ({safe_to_refresh: false, reason_code});
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return result('NOT_GEMINI');
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0
      && r.width > 0 && r.height > 0;
  };
  if ([...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"]')].some(visible))
    return result('MODAL_PRESENT');
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')].filter(visible);
  if (editors.length !== 1) return result('COMPOSER_UNAVAILABLE');
  const editor = editors[0], area = editor.closest('.input-area');
  if (!area || !visible(area) || editor.getAttribute('aria-disabled') === 'true'
      || editor.closest('[inert],[aria-disabled="true"]')) return result('COMPOSER_UNAVAILABLE');
  if (editor.textContent !== '' || area.querySelector('img,video,audio,iframe,object,embed'))
    return result('DRAFT_NOT_EMPTY');
  if ([...area.querySelectorAll('input[type="file"]')].some(input => input.files && input.files.length > 0))
    return result('DRAFT_NOT_EMPTY');
  return {safe_to_refresh: true, reason_code: 'EMPTY_COMPOSER'};
}"""

GEMINI_SESSION_REFRESH_REASONS = frozenset({
  'EMPTY_COMPOSER', 'NOT_GEMINI', 'MODAL_PRESENT', 'COMPOSER_UNAVAILABLE', 'DRAFT_NOT_EMPTY',
})

GEMINI_AUTH_REASONS = frozenset({
  'ACCOUNT_CONTROLS_PRESENT', 'SIGN_IN_CONTROL_PRESENT', 'PROOF_INCOMPLETE',
  'PROBE_UNAVAILABLE', 'NOT_GEMINI',
})


async def inspect_gemini_auth_status(page) -> dict[str, str]:
    """Return a fixed, privacy-safe sign-in state without returning page text."""
    try:
        result = await page.evaluate(GEMINI_AUTH_STATUS)
    except Exception:
        result = None
    if (not isinstance(result, dict) or set(result) != {'status', 'reason_code'}
            or result.get('status') not in {'signed_in', 'signed_out', 'unknown'}
            or result.get('reason_code') not in GEMINI_AUTH_REASONS):
        return {'status': 'unknown', 'reason_code': 'PROBE_UNAVAILABLE'}
    if (result['status'] == 'signed_in' and result['reason_code'] != 'ACCOUNT_CONTROLS_PRESENT'
            or result['status'] == 'signed_out' and result['reason_code'] != 'SIGN_IN_CONTROL_PRESENT'
            or result['status'] == 'unknown' and result['reason_code'] not in {'PROOF_INCOMPLETE', 'PROBE_UNAVAILABLE', 'NOT_GEMINI'}):
        return {'status': 'unknown', 'reason_code': 'PROBE_UNAVAILABLE'}
    return {'status': result['status'], 'reason_code': result['reason_code']}

# Diagnostics expose bounded aggregate structure only. They never return page
# values, text, labels, URLs or user/account information.
GEMINI_DIAGNOSTICS = r"""() => {
  const count = selector => Math.min(99, document.querySelectorAll(selector).length);
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')];
  const labels = new Set(['send message', 'send prompt', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u043f\u0440\u043e\u0441', '\u53d1\u9001\u6d88\u606f']);
  const buttons = [...document.querySelectorAll('.input-area button')].filter(b => labels.has((b.getAttribute('aria-label') || '').trim().toLowerCase()));
  return {gemini_main_frame: window === window.top && location.origin === 'https://gemini.google.com',
    input_area_count: count('.input-area'), input_container_count: count('.input-container'),
    composer_count: Math.min(99, editors.length), send_button_count: Math.min(99, buttons.length),
    active_composer: editors.length === 1 && document.activeElement === editors[0],
    dialog_count: count('dialog[open],[role="dialog"],[aria-modal="true"]'),
    known_send_label: buttons.some(b => labels.has((b.getAttribute('aria-label') || '').trim().toLowerCase()))};
}"""

# Only booleans leave the page. No prompt text, account information or model code
# is read. The tightly scoped selector intentionally fails on unknown redesigns.
GEMINI_SUBMIT_CHECK = r"""({kind, x, y}) => {
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return false;
  const labels = new Set(['send message', 'send prompt', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u043f\u0440\u043e\u0441', '\u53d1\u9001\u6d88\u606f']);
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
  };
  if ([...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"]')].some(visible)) return false;
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')].filter(visible);
  if (editors.length !== 1) return false;
  const editor = editors[0];
  const area = editor.closest('.input-area');
  if (!area || !visible(area) || area.closest('form')?.querySelector('input[type="password"]')) return false;
  if (editor.getAttribute('aria-disabled') === 'true' || editor.closest('[inert],[aria-disabled="true"]')) return false;
  if (kind === 'enter') {
    const r = editor.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return document.activeElement === editor && !!hit && (hit === editor || editor.contains(hit));
  }
  const hit = document.elementFromPoint(x, y);
  const button = hit?.closest('button');
  if (!button || !area.contains(button) || !visible(button) || button.disabled || button.getAttribute('aria-disabled') === 'true') return false;
  if (!labels.has((button.getAttribute('aria-label') || '').trim().toLowerCase())) return false;
  const buttons = [...area.querySelectorAll('button')].filter(b => visible(b) && labels.has((b.getAttribute('aria-label') || '').trim().toLowerCase()));
  if (buttons.length !== 1 || buttons[0] !== button) return false;
  return true;

}"""

# The fast path is deliberately a fixed DOM contract. The prompt itself is
# only used for equality checks inside the page; none of these probes returns
# editor contents, labels, messages, URLs, or account data.
GEMINI_PROMPT_PREFLIGHT = r"""({text}) => {
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return 'blocked';
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
  };
  if ([...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"]')].some(visible)) return 'blocked';
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')].filter(visible);
  if (editors.length === 0) return 'editor_not_ready';
  if (editors.length !== 1) return 'blocked';
  const editor = editors[0], area = editor.closest('.input-area');
  if (!area || !visible(area)) return 'editor_not_ready';
  if (editor.getAttribute('aria-disabled') === 'true' || editor.closest('[inert],[aria-disabled="true"]')) return 'blocked';
  if (area.closest('form')?.querySelector('input[type="password"]')) return 'blocked';
  if (area.querySelector('img,video,audio,iframe,object,embed')) return 'blocked';
  if ([...area.querySelectorAll('input[type="file"]')].some(input => input.files && input.files.length > 0)) return 'blocked';
  const empty = editor.textContent === '';
  const nodes = [...editor.childNodes];
  const blocks = nodes.length > 0 && nodes.every(n => n.nodeType === 1 && ['P', 'DIV'].includes(n.tagName));
  const plainNode = n => {
    if (n.nodeType === 3) return n.nodeValue || '';
    if (n.nodeType !== 1 || n.tagName === 'BR') return null;
    if (!['SPAN', 'B', 'STRONG', 'EM', 'I', 'U', 'S', 'CODE', 'A', 'MARK', 'SMALL', 'SUB', 'SUP'].includes(n.tagName)) return null;
    const parts = [...n.childNodes].map(plainNode);
    return parts.some(part => part === null) ? null : parts.join('');
  };
  let content = null;
  if (blocks) {
    const lines = nodes.map(n => n.childNodes.length === 1 && n.firstChild.nodeType === 1 && n.firstChild.tagName === 'BR'
      ? '' : (() => { const parts = [...n.childNodes].map(plainNode); return parts.some(part => part === null) ? null : parts.join(''); })());
    if (!lines.some(line => line === null)) content = lines.join('\n');
  } else if (nodes.every(n => n.nodeType === 3)) {
    content = nodes.map(n => n.nodeValue || '').join('');
  }
  if (content === null) return 'blocked';
  const matches = content === text;
  if (!empty && !matches) return 'draft_mismatch';
  const r = editor.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (!hit || (hit !== editor && !editor.contains(hit))) return 'blocked';
  return {state: !empty && matches ? 'same' : 'empty', x, y};
}"""

GEMINI_PROMPT_VERIFY = r"""({text}) => {
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return false;
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')];
  if (editors.length !== 1 || !editors[0].isConnected) return false;
  const nodes = [...editors[0].childNodes];
  const blocks = nodes.length > 0 && nodes.every(n => n.nodeType === 1 && ['P', 'DIV'].includes(n.tagName));
  const plainNode = n => {
    if (n.nodeType === 3) return n.nodeValue || '';
    if (n.nodeType !== 1 || n.tagName === 'BR') return null;
    if (!['SPAN', 'B', 'STRONG', 'EM', 'I', 'U', 'S', 'CODE', 'A', 'MARK', 'SMALL', 'SUB', 'SUP'].includes(n.tagName)) return null;
    const parts = [...n.childNodes].map(plainNode);
    return parts.some(part => part === null) ? null : parts.join('');
  };
  let content = null;
  if (blocks) {
    const lines = nodes.map(n => n.childNodes.length === 1 && n.firstChild.nodeType === 1 && n.firstChild.tagName === 'BR'
      ? '' : (() => { const parts = [...n.childNodes].map(plainNode); return parts.some(part => part === null) ? null : parts.join(''); })());
    if (!lines.some(line => line === null)) content = lines.join('\n');
  } else if (nodes.every(n => n.nodeType === 3)) {
    content = nodes.map(n => n.nodeValue || '').join('');
  }
  return content === text;
}"""

GEMINI_PROMPT_FOCUS_VERIFY = r"""() => {
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return false;
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
  };
  if ([...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"]')].some(visible)) return false;
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')].filter(visible);
  return editors.length === 1 && document.activeElement === editors[0]
    && !!editors[0].closest('.input-area') && !editors[0].closest('[inert],[aria-disabled="true"]');
}"""

GEMINI_PROMPT_READY = r"""() => {
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return false;
  const labels = new Set(['send message', 'send prompt', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u043f\u0440\u043e\u0441', '\u53d1\u9001\u6d88\u606f']);
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
  };
  if ([...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"]')].some(visible)) return false;
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')].filter(visible);
  if (editors.length !== 1 || document.activeElement !== editors[0]) return false;
  const area = editors[0].closest('.input-area');
  if (!area || !visible(area) || editors[0].closest('[inert],[aria-disabled="true"]')) return false;
  if (area.querySelector('img,video,audio,iframe,object,embed')) return false;
  if ([...area.querySelectorAll('input[type="file"]')].some(input => input.files && input.files.length > 0)) return false;
  const buttons = [...area.querySelectorAll('button')].filter(b => visible(b) && labels.has((b.getAttribute('aria-label') || '').trim().toLowerCase()));
  if (buttons.length !== 1 || buttons[0].disabled || buttons[0].getAttribute('aria-disabled') === 'true') return false;
  return true;

}"""

GEMINI_PROMPT_SEND_TARGET = r"""() => {
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return null;
  const labels = new Set(['send message', 'send prompt', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435', '\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u043f\u0440\u043e\u0441', '\u53d1\u9001\u6d88\u606f']);
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
  };
  if ([...document.querySelectorAll('dialog[open],[role="dialog"],[aria-modal="true"]')].some(visible)) return null;
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')].filter(visible);
  if (editors.length !== 1 || document.activeElement !== editors[0]) return null;
  const area = editors[0].closest('.input-area');
  if (!area || !visible(area) || editors[0].closest('[inert],[aria-disabled="true"]')) return null;
  if (area.querySelector('img,video,audio,iframe,object,embed')) return null;
  if ([...area.querySelectorAll('input[type="file"]')].some(input => input.files && input.files.length > 0)) return null;
  const buttons = [...area.querySelectorAll('button')].filter(b => visible(b) && labels.has((b.getAttribute('aria-label') || '').trim().toLowerCase()));
  if (buttons.length !== 1 || buttons[0].disabled || buttons[0].getAttribute('aria-disabled') === 'true') return null;
  const button = buttons[0], r = button.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (!hit || (hit !== button && !button.contains(hit))) return null;
  return {x, y};
}"""

GEMINI_PROMPT_ACK = r"""() => {
  if (window !== window.top || location.origin !== 'https://gemini.google.com') return false;
  const visible = e => {
    if (!e || !e.isConnected) return false;
    const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.display !== 'none' && s.visibility === 'visible' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
  };
  const editors = [...document.querySelectorAll('rich-textarea .ql-editor[contenteditable="true"]')].filter(visible);
  return editors.length === 1 && editors[0].closest('.input-area') && editors[0].textContent === '';
}"""


async def is_gemini_submit(page, action: dict) -> bool:
    if action.get("kind") == "press_key" and action.get("key") == "Enter":
        args = {"kind": "enter", "x": 0, "y": 0}
    elif action.get("kind") == "click" and action.get("button", "left") == "left" and action.get("count", 1) == 1:
        args = {"kind": "click", "x": action["x"], "y": action["y"]}
    else:
        return False
    try:
        return await page.evaluate(GEMINI_SUBMIT_CHECK, args) is True
    except Exception:
        return False
