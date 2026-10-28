import pytest
import httpx

from worker.app import BrowserRuntime, WorkerError, create_app


class Locator:
    def __init__(self, count=1, *, checked=False, enabled=True, visible=True, text=''):
        self.n, self.checked, self.enabled, self.visible, self.text = count, checked, enabled, visible, text
    async def count(self): return self.n
    async def is_checked(self, **kwargs): return self.checked
    async def is_enabled(self, **kwargs): return self.enabled
    async def is_visible(self, **kwargs): return self.visible
    async def inner_text(self, **kwargs): return self.text


class Page:
    def __init__(self): self.nodes = {}
    def locator(self, selector): return self.nodes.get(selector, Locator(0))


class DiagnosticPage(Page):
    def __init__(self, result):
        super().__init__(); self.result = result; self.script = None
    async def evaluate(self, script, *args):
        self.script = script
        return self.result


class Adapter:
    def __init__(self, url): self.current_url = url
    async def url(self): return self.current_url


class StyleDiagnosticPage(Page):
    def __init__(self):
        super().__init__(); self.script = None; self.argument = None
    async def evaluate(self, script, argument):
        self.script = script; self.argument = argument
        variant = argument["variant"]
        family, weight = {
            "baseline": ("Roboto, Arial, sans-serif", "400"),
            "arial_normal": ("Arial, sans-serif", "400"),
            "arial_medium": ("Arial, sans-serif", "500"),
            "arial_thin100": ("Arial, sans-serif", "100"),
            "system_normal": ("system-ui, sans-serif", "400"),
            "system_bold": ("system-ui, sans-serif", "700"),
            "system_thin100": ("system-ui, sans-serif", "100"),
            "baseline_normal": ("Roboto, Arial, sans-serif", "400"),
            "monospace": ("monospace", "400"),
        }[variant]
        return {"variant": variant, "heading_selector": "main-header", "heading_text": "Web Tables",
                "font_family": family, "font_weight": weight}


async def request(app, method, url, *, token="worker-test", json=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return await client.request(method, url, headers=headers, json=json)


@pytest.mark.asyncio
async def test_typed_element_verification_and_no_content_leaks(tmp_path):
    page = Page(); page.nodes['#box'] = Locator(checked=True, text='synthetic output')
    runtime = BrowserRuntime(tmp_path, page=page, adapter=object(), test_mode=True)
    conditions = [{'kind':'element_checked','selector':'#box','checked':True},
                  {'kind':'element_enabled','selector':'#box','enabled':True},
                  {'kind':'element_visible','selector':'#box','visible':True},
                  {'kind':'element_text_contains','selector':'#box','text':'synthetic'},
                  {'kind':'element_visible','selector':'#missing','visible':False}]
    result = await runtime.verify(conditions)
    assert result['verified'] and len(result['checks']) == 5
    assert 'synthetic' not in str(result) and '#box' not in str(result)
    page.nodes['#box'].n = 2
    assert not (await runtime.verify(conditions))['verified']
    await runtime.close()


@pytest.mark.asyncio
async def test_typed_checked_condition_accepts_unique_aria_checkbox_state(tmp_path):
    page = Page()
    page.nodes['[role="checkbox"][aria-label="Desktop"]'] = Locator(checked=True)
    page.nodes['[role="checkbox"][aria-label="Home"]'] = Locator(checked=False)
    runtime = BrowserRuntime(tmp_path, page=page, adapter=object(), test_mode=True)
    checks = await runtime.verify([
        {"kind": "element_checked", "selector": '[role="checkbox"][aria-label="Desktop"]', "checked": True},
        {"kind": "element_checked", "selector": '[role="checkbox"][aria-label="Home"]', "checked": False},
    ])
    assert checks["verified"] and all(check["passed"] for check in checks["checks"])
    page.nodes['[role="checkbox"][aria-label="Desktop"]'].n = 2
    ambiguous = await runtime.verify([
        {"kind": "element_checked", "selector": '[role="checkbox"][aria-label="Desktop"]', "checked": True},
    ])
    assert not ambiguous["verified"]
    class MixedCheckbox:
        async def count(self): return 1
        async def is_checked(self, **kwargs): raise ValueError("mixed state is not boolean")
    page.nodes['[role="checkbox"][aria-label="Mixed"]'] = MixedCheckbox()
    mixed = await runtime.verify([
        {"kind": "element_checked", "selector": '[role="checkbox"][aria-label="Mixed"]', "checked": False},
    ])
    assert not mixed["verified"]
    await runtime.close()


@pytest.mark.asyncio
async def test_invalid_conditions_rejected_and_locator_errors_fail_closed(tmp_path):
    page = Page(); runtime = BrowserRuntime(tmp_path, page=page, adapter=object(), test_mode=True)
    for condition in ({'kind':'element_checked','selector':'#x','checked':1},
                      {'kind':'element_visible','selector':'#x','visible':True,'extra':0},
                      {'kind':'element_text_contains','selector':'x'*257,'text':'x'},
                      {'kind':'element_text_contains','selector':'#x','text':'x'*10001}):
        with pytest.raises(WorkerError): await runtime.verify([condition])
    class Broken:
        async def count(self): raise ValueError('private-text')
    page.nodes['#broken'] = Broken()
    result = await runtime.verify([{'kind':'element_visible','selector':'#broken','visible':True}])
    assert not result['verified'] and 'private-text' not in str(result)
    await runtime.close()


def diagnostic_payload():
    return {"input_count": 1, "truncated": False,
            "selector_counts": {"checkbox_inputs": 1, "desktop_id": 1, "desktop_labels": 1, "aria_checkboxes": 0},
            "role_checkbox_count": 1, "role_checkboxes_truncated": False,
            "role_checkboxes": [{"tag": "span", "id": "desktop-role", "role": "checkbox",
                                 "aria_label": "", "aria_checked": "true", "checked": True,
                                 "visible": True, "class_names": ["rct-checkbox"],
                                 "accessible_label": "Desktop", "static_label": "Desktop",
                                 "selector": '[role="checkbox"]#desktop-role'}],
            "inputs": [{
                "tag": "input", "type": "checkbox", "id": "tree-node-desktop", "name": "",
                "aria_label": "", "pattern": "", "checked": True, "validity_valid": True,
                "aria_checked": None, "class_names": ["rct-checkbox"], "labels": ["Desktop"],
            }]}


@pytest.mark.asyncio
async def test_demoqa_diagnostics_disabled_by_default_and_bearer_protected(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DEMOQA_DIAGNOSTICS", raising=False)
    page = DiagnosticPage(diagnostic_payload())
    runtime = BrowserRuntime(tmp_path, page=page, adapter=Adapter("https://demoqa.com/checkbox"), test_mode=True)
    app = create_app(runtime, token="worker-test")
    disabled = await request(app, "GET", "/diagnostics/demoqa-inputs")
    unauthenticated = await request(app, "GET", "/diagnostics/demoqa-inputs", token=None)
    assert disabled.status_code == 404
    assert unauthenticated.status_code == 401
    assert page.script is None
    await runtime.close()


@pytest.mark.asyncio
async def test_demoqa_diagnostics_returns_bounded_metadata_only_on_exact_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMOQA_DIAGNOSTICS", "1")
    page = DiagnosticPage(diagnostic_payload())
    page.nodes['[role="checkbox"]#desktop-role'] = Locator(checked=True)
    adapter = Adapter("https://demoqa.com/checkbox")
    runtime = BrowserRuntime(tmp_path, page=page, adapter=adapter, test_mode=True)
    app = create_app(runtime, token="worker-test")
    response = await request(app, "GET", "/diagnostics/demoqa-inputs")
    assert response.status_code == 200
    assert response.json() == diagnostic_payload()
    assert "input_value" not in page.script and "document.body" not in page.script
    assert "value:" not in page.script and "input.value" not in page.script
    assert "location.origin !== 'https://demoqa.com' || window !== window.top" in page.script
    assert ".replace(/\\s+/g" in page.script
    assert "role_checkboxes" in response.json()
    assert response.json()["role_checkboxes"][0]["accessible_label"] == "Desktop"
    assert response.json()["role_checkboxes"][0]["selector"] == '[role="checkbox"]#desktop-role'
    assert "textContent" not in response.json() and "value" not in response.json()
    adapter.current_url = "https://example.com/checkbox"
    wrong_origin = await request(app, "GET", "/diagnostics/demoqa-inputs")
    assert wrong_origin.status_code == 404 and "tree-node-desktop" not in wrong_origin.text
    await runtime.close()


@pytest.mark.asyncio
async def test_demoqa_diagnostics_rejects_unbounded_or_value_bearing_data(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "debug")
    monkeypatch.setenv("DEMOQA_DIAGNOSTICS", "1")
    invalid = diagnostic_payload()
    invalid["inputs"][0]["value"] = "synthetic-secret"
    page = DiagnosticPage(invalid)
    runtime = BrowserRuntime(tmp_path, page=page, adapter=Adapter("https://demoqa.com"), test_mode=True)
    app = create_app(runtime, token="worker-test")
    response = await request(app, "GET", "/diagnostics/demoqa-inputs")
    assert response.status_code == 503 and "synthetic-secret" not in response.text
    await runtime.close()


@pytest.mark.asyncio
async def test_demoqa_diagnostics_rejects_role_checkbox_page_text_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMOQA_DIAGNOSTICS", "1")
    invalid = diagnostic_payload()
    invalid["role_checkboxes"][0]["textContent"] = "page text"
    page = DiagnosticPage(invalid)
    runtime = BrowserRuntime(tmp_path, page=page, adapter=Adapter("https://demoqa.com"), test_mode=True)
    app = create_app(runtime, token="worker-test")
    response = await request(app, "GET", "/diagnostics/demoqa-inputs")
    assert response.status_code == 503 and "page text" not in response.text
    await runtime.close()


def font_diagnostic_payload():
    return {
        "heading_selector": "main-header", "heading_match_count": 1,
        "header_text": "Web Tables", "font_family": "Roboto, Arial, sans-serif",
        "fonts_status": "loaded", "fonts_check": True,
        "font_faces_count": 1, "font_faces_truncated": False,
        "font_faces": [{"family": "Roboto", "status": "loaded"}],
        "font_resources_count": 1, "font_resources_truncated": False,
        "font_resources": [{"origin": "https://demoqa.com", "path": "/fonts/roboto.woff2"}],
    }


@pytest.mark.asyncio
async def test_demoqa_font_diagnostics_disabled_by_default_and_bearer_protected(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DEMOQA_DIAGNOSTICS", raising=False)
    page = DiagnosticPage(font_diagnostic_payload())
    runtime = BrowserRuntime(tmp_path, page=page, adapter=Adapter("https://demoqa.com/webtables"), test_mode=True)
    app = create_app(runtime, token="worker-test")
    disabled = await request(app, "GET", "/diagnostics/demoqa-fonts")
    unauthenticated = await request(app, "GET", "/diagnostics/demoqa-fonts", token=None)
    assert disabled.status_code == 404 and unauthenticated.status_code == 401
    assert page.script is None
    await runtime.close()


@pytest.mark.asyncio
async def test_demoqa_font_diagnostics_returns_only_bounded_title_and_font_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMOQA_DIAGNOSTICS", "1")
    page = DiagnosticPage(font_diagnostic_payload())
    adapter = Adapter("https://demoqa.com/webtables")
    runtime = BrowserRuntime(tmp_path, page=page, adapter=adapter, test_mode=True)
    app = create_app(runtime, token="worker-test")
    response = await request(app, "GET", "/diagnostics/demoqa-fonts")
    assert response.status_code == 200 and response.json() == font_diagnostic_payload()
    assert "location.origin !== 'https://demoqa.com' || window !== window.top" in page.script
    assert ".main-header" in page.script and "document.fonts" in page.script
    assert "document.body" not in page.script and "input.value" not in page.script
    assert "search" not in page.script and "hash" not in page.script
    adapter.current_url = "https://example.com/webtables"
    wrong_origin = await request(app, "GET", "/diagnostics/demoqa-fonts")
    assert wrong_origin.status_code == 404 and "Web Tables" not in wrong_origin.text
    await runtime.close()


@pytest.mark.asyncio
async def test_demoqa_font_diagnostics_rejects_unbounded_fields_and_resource_queries(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "debug")
    monkeypatch.setenv("DEMOQA_DIAGNOSTICS", "1")
    invalid = font_diagnostic_payload()
    invalid["font_resources"][0]["path"] = "/fonts/site.woff2?private=secret"
    page = DiagnosticPage(invalid)
    runtime = BrowserRuntime(tmp_path, page=page, adapter=Adapter("https://demoqa.com"), test_mode=True)
    app = create_app(runtime, token="worker-test")
    response = await request(app, "GET", "/diagnostics/demoqa-fonts")
    assert response.status_code == 503 and "private=secret" not in response.text
    await runtime.close()


@pytest.mark.asyncio
async def test_demoqa_font_style_probe_accepts_only_fixed_variants(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMOQA_DIAGNOSTICS", "1")
    page = StyleDiagnosticPage()
    runtime = BrowserRuntime(tmp_path, page=page, adapter=Adapter("https://demoqa.com/webtables"), test_mode=True)
    app = create_app(runtime, token="worker-test")
    variants = ("baseline", "arial_normal", "arial_medium", "system_normal", "system_bold", "monospace",
                "system_thin100", "arial_thin100", "baseline_normal")
    for variant in variants:
        response = await request(app, "POST", "/diagnostics/demoqa-font-style", json={"variant": variant})
        assert response.status_code == 200
        assert response.json()["variant"] == variant
        assert response.json()["heading_text"] == "Web Tables"
        assert page.argument == {"variant": variant}
        assert "location.origin !== 'https://demoqa.com' || window !== window.top" in page.script
        assert "document.body" not in page.script
    assert "style.setProperty('font-family'" in page.script
    assert "style.setProperty('font-weight'" in page.script
    assert "family: null, weight: '400'" in page.script
    assert "system_thin100: { family: 'system-ui, sans-serif', weight: '100' }" in page.script
    assert "arial_thin100: { family: 'Arial, sans-serif', weight: '100' }" in page.script
    for payload in ({"variant": "arial_normal", "css": "body{}"},
                    {"variant": "window.alert(1)"}, {}, {"variant": 1}):
        rejected = await request(app, "POST", "/diagnostics/demoqa-font-style", json=payload)
        assert rejected.status_code == 422
    assert page.argument == {"variant": "baseline_normal"}
    await runtime.close()


@pytest.mark.asyncio
async def test_demoqa_font_style_probe_remains_off_in_production_and_on_other_origins(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DEMOQA_DIAGNOSTICS", raising=False)
    page = StyleDiagnosticPage()
    adapter = Adapter("https://demoqa.com/webtables")
    runtime = BrowserRuntime(tmp_path, page=page, adapter=adapter, test_mode=True)
    app = create_app(runtime, token="worker-test")
    disabled = await request(app, "POST", "/diagnostics/demoqa-font-style", json={"variant": "baseline"})
    assert disabled.status_code == 404 and page.script is None
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMOQA_DIAGNOSTICS", "1")
    adapter.current_url = "https://example.com/webtables"
    wrong_origin = await request(app, "POST", "/diagnostics/demoqa-font-style", json={"variant": "baseline"})
    assert wrong_origin.status_code == 404 and page.script is None
    await runtime.close()
