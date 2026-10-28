import pytest

import worker.app as worker_app
from worker.app import BrowserRuntime


class Request:
    def __init__(self, url, resource_type="script", method="GET", navigation=False, frame=None, headers=None):
        self.url = url
        self.resource_type = resource_type
        self.method = method
        self.navigation = navigation
        self.frame = frame
        self.headers = headers or {}

    def is_navigation_request(self):
        return self.navigation


class Route:
    def __init__(self, *args, **kwargs):
        self.request = Request(*args, **kwargs)
        self.result = None

    async def continue_(self):
        self.result = "continued"

    async def abort(self, reason):
        self.result = reason


class Frame:
    def __init__(self, url, parent_frame=None):
        self.url = url
        self.parent_frame = parent_frame


class Page:
    def __init__(self, url, main_frame):
        self.url = url
        self.main_frame = main_frame


@pytest.mark.asyncio
async def test_gemini_cdn_only_public_read_subresources(tmp_path, monkeypatch):
    async def public(host):
        return host != "www.gstatic.com" or public.allow_www
    public.allow_www = True
    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    for host in ("www.gstatic.com", "gemini.gstatic.com"):
        for method in ("GET", "HEAD"):
            route = Route(f"https://{host}/asset.js?secret=not-logged", method=method)
            await runtime._route_guard(route)
            assert route.result == "continued"
        for route in (Route(f"https://{host}/", method="POST"),
                      Route(f"https://{host}/", resource_type="document", navigation=True),
                      Route(f"https://{host}/", resource_type="script", navigation=True),
                      Route(f"https://{host}/", resource_type="document")):
            await runtime._route_guard(route)
            assert route.result == "blockedbyclient"
    blocked = Route("https://accounts.google.com/private?secret=not-logged", resource_type="document", navigation=True)
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"
    public.allow_www = False
    blocked = Route("https://www.gstatic.com/asset")
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"
    assert any(row["reason"] == "non_public_or_unresolved_host" for row in runtime.status_base()["blocked_resources"])
    runtime._route_origins = frozenset({"https://unrelated.example"})
    blocked = Route("https://gemini.gstatic.com/script")
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"
    allowed = Route("https://unrelated.example/api", method="POST", resource_type="fetch")
    await runtime._route_guard(allowed)
    assert allowed.result == "continued"
    runtime.egress_ready = False
    blocked = Route("https://unrelated.example/api")
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"
    runtime.egress_ready = True
    runtime.route_generation = 0
    blocked = Route("https://unrelated.example/api")
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"


@pytest.mark.asyncio
async def test_blocked_telemetry_bounded_private_and_reset(tmp_path, monkeypatch):
    async def public(_host):
        return True
    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    for _ in range(2):
        await runtime._route_guard(Route("https://blocked.example/private?token=secret", resource_type="fetch"))
    for i in range(40):
        await runtime._route_guard(Route(f"https://host{i}.example/path?secret=x"))
    rows = runtime.status_base()["blocked_resources"]
    assert len(rows) == 32
    assert rows[0] == {"origin": "https://blocked.example", "resource_type": "fetch", "reason": "origin_not_allowed",
                       "path_family": "other", "frame": "unknown", "method": "GET", "count": 2}
    assert all(set(row) == {"origin", "resource_type", "reason", "path_family", "frame", "method", "count"} for row in rows)
    assert "secret" not in str(rows) and "private" not in str(rows)

    class Adapter:
        async def goto(self, *_args, **_kwargs):
            return {"url": "https://gemini.google.com/app", "status": 200}
        async def url(self):
            return "https://gemini.google.com/app"

    runtime._adapter = Adapter()
    await runtime._execute_action({"kind": "navigate", "url": "https://gemini.google.com/app"},
                                  frozenset({"https://gemini.google.com"}), manual=True, allow_write=True, expected_epoch=0)
    assert runtime.status_base()["blocked_resources"] == []
    await runtime._route_guard(Route("https://blocked.example/path"))
    runtime._page = object()
    runtime._started = True
    runtime.gate("agent")
    await runtime.reset_view(runtime.control_epoch)
    assert runtime.status_base()["blocked_resources"] == []
    await runtime._route_guard(Route("https://blocked.example/path"))
    await runtime.close()
    assert runtime.status_base()["blocked_resources"] == []


@pytest.mark.asyncio
async def test_auth_block_telemetry_uses_only_static_path_and_frame_labels(tmp_path, monkeypatch):
    async def public(_host):
        return True
    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    main = Frame("https://gemini.google.com/app")
    sub = Frame("https://gemini.google.com/frame")
    runtime._page = Page("https://gemini.google.com/app", main)

    await runtime._route_guard(Route(
        "https://accounts.google.com/ServiceLogin?email=private-account@example.test&token=secret-query",
        resource_type="document", method="GET", navigation=True, frame=main,
    ))
    await runtime._route_guard(Route(
        "https://ogs.google.com/u/12/widget/app?authuser=private-account@example.test",
        resource_type="document", method="GET", frame=sub,
    ))
    await runtime._route_guard(Route(
        "https://accounts.google.com/private/path/123?secret=private-query",
        resource_type="fetch", method="POST", frame=None,
    ))

    rows = runtime.status_base()["blocked_resources"]
    labels = {(row["origin"], row["path_family"], row["frame"]) for row in rows}
    assert labels == {
        ("https://accounts.google.com", "service_login", "main"),
        ("https://ogs.google.com", "ogs_widget_app", "subframe"),
        ("https://accounts.google.com", "other", "unknown"),
    }
    serialized = str(rows)
    for sensitive in ("ServiceLogin", "private-account", "secret-query", "/private/path", "123", "authuser"):
        assert sensitive not in serialized
    assert len(rows) <= 32


@pytest.mark.asyncio
async def test_gemini_font_exception_is_scoped_and_read_only(tmp_path, monkeypatch):
    async def public(host):
        return host != "fonts.gstatic.com" or public.allow_fonts
    public.allow_fonts = True
    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    for method in ("GET", "HEAD"):
        route = Route("https://fonts.gstatic.com/s/googlesans/test.ttf", resource_type="font", method=method)
        await runtime._route_guard(route)
        assert route.result == "continued"
    for route in (
        Route("https://fonts.gstatic.com/font", resource_type="font", method="POST"),
        Route("https://fonts.gstatic.com/font", resource_type="font", navigation=True),
        Route("https://fonts.gstatic.com/font", resource_type="document"),
        Route("https://fonts.gstatic.com/font", resource_type="stylesheet"),
        Route("https://fonts.gstatic.com/font", resource_type="script"),
        Route("https://fonts.gstatic.com/font", resource_type="xhr"),
        Route("https://fonts.gstatic.com/font", resource_type="fetch"),
        Route("https://fonts.googleapis.com/font", resource_type="font"),
        Route("https://other.fonts.gstatic.com/font", resource_type="font"),
        Route("https://fonts.gstatic.com.evil.example/font", resource_type="font"),
        Route("https://fonts.gstatic.com:8443/font", resource_type="font"),
        Route("http://fonts.gstatic.com/font", resource_type="font"),
        Route("https://accounts.google.com/", resource_type="font"),
    ):
        await runtime._route_guard(route)
        assert route.result == "blockedbyclient"
    runtime._route_origins = frozenset({"https://unrelated.example"})
    route = Route("https://fonts.gstatic.com/font", resource_type="font")
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    public.allow_fonts = False
    route = Route("https://fonts.gstatic.com/font", resource_type="font")
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"
    assert any(row["origin"] == "https://fonts.gstatic.com" and row["reason"] == "non_public_or_unresolved_host"
               for row in runtime.status_base()["blocked_resources"])
    public.allow_fonts = True
    runtime.egress_ready = False
    route = Route("https://fonts.gstatic.com/font", resource_type="font")
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"
    runtime.egress_ready = True
    runtime.route_generation = 0
    route = Route("https://fonts.gstatic.com/font", resource_type="font")
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"


@pytest.mark.asyncio
async def test_gemini_auth_apis_allow_only_exact_origins_from_canonical_main_frame(tmp_path, monkeypatch):
    async def public(_host):
        return not public.blocked
    public.blocked = False
    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    main_frame = Frame("https://gemini.google.com/app")
    runtime._page = Page("https://gemini.google.com/app", main_frame)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    exact_origins = (
        "https://geminiweb-pa.clients6.google.com",
        "https://waa-pa.clients6.google.com",
        "https://signaler-pa.clients6.google.com",
    )
    for origin in exact_origins:
        for resource_type in ("xhr", "fetch", "other"):
            for method in ("GET", "HEAD", "POST"):
                route = Route(f"{origin}/rpc?private=not-logged", resource_type=resource_type,
                              method=method, frame=main_frame)
                await runtime._route_guard(route)
                assert route.result == "continued"

    valid_preflight = Route("https://signaler-pa.clients6.google.com/rpc", resource_type="other",
        method="OPTIONS", frame=main_frame, headers={
            "origin": "https://gemini.google.com", "access-control-request-method": "POST",
        })
    await runtime._route_guard(valid_preflight)
    assert valid_preflight.result == "continued"

    for url, resource_type, method, navigation, frame, page_url, route_origins in (
        ("https://evil-geminiweb-pa.clients6.google.com/rpc", "xhr", "POST", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://geminiweb-pa.clients6.google.com:8443/rpc", "xhr", "POST", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("http://waa-pa.clients6.google.com/rpc", "fetch", "POST", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "other", "OPTIONS", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "xhr", "DELETE", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "xhr", "POST", True, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "document", "GET", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "script", "GET", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "fetch", "POST", False, Frame("https://gemini.google.com/frame"),
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "fetch", "POST", False, None,
         "https://gemini.google.com/app", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "fetch", "POST", False, main_frame,
         "https://accounts.google.com/", frozenset({"https://gemini.google.com"})),
        ("https://waa-pa.clients6.google.com/rpc", "fetch", "POST", False, main_frame,
         "https://gemini.google.com/app", frozenset({"https://unrelated.example"})),
    ):
        runtime._page = Page(page_url, main_frame)
        runtime._route_origins = route_origins
        route = Route(url, resource_type=resource_type, method=method, navigation=navigation, frame=frame)
        await runtime._route_guard(route)
        assert route.result == "blockedbyclient"

    runtime._page = Page("https://gemini.google.com/app", main_frame)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    for method in ("GET", "HEAD", "POST"):
        route = Route("https://signaler-pa.clients6.google.com/rpc", resource_type="other",
                      method=method, frame=main_frame)
        await runtime._route_guard(route)
        assert route.result == "continued"
    invalid_preflights = (
        ({}, False),
        ({"origin": "https://accounts.google.com", "access-control-request-method": "POST"}, False),
        ({"origin": "https://gemini.google.com", "access-control-request-method": "DELETE"}, False),
        ({"origin": "https://gemini.google.com", "access-control-request-method": "POST"}, True),
    )
    for headers, navigation in invalid_preflights:
        route = Route("https://signaler-pa.clients6.google.com/rpc", resource_type="other",
                      method="OPTIONS", navigation=navigation, frame=main_frame, headers=headers)
        await runtime._route_guard(route)
        assert route.result == "blockedbyclient"
    route = Route("https://signaler-pa.clients6.google.com/rpc", resource_type="other",
                  method="POST", frame=Frame("https://gemini.google.com/frame", main_frame))
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"
    route = Route("https://signaler-pa.clients6.google.com/rpc", resource_type="other",
                  method="POST", frame=main_frame, headers={"origin": "https://accounts.google.com"})
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"

    runtime._page = Page("https://gemini.google.com/app", main_frame)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    runtime.egress_ready = False
    blocked = Route("https://waa-pa.clients6.google.com/rpc", resource_type="fetch", method="POST", frame=main_frame)
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"
    runtime.egress_ready = True
    runtime.route_generation = 0
    blocked = Route("https://geminiweb-pa.clients6.google.com/rpc", resource_type="xhr", method="GET", frame=main_frame)
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"
    runtime.route_generation = 1
    public.blocked = True
    blocked = Route("https://waa-pa.clients6.google.com/rpc?private=not-logged", resource_type="fetch",
                    method="POST", frame=main_frame)
    await runtime._route_guard(blocked)
    assert blocked.result == "blockedbyclient"
    assert runtime.status_base()["blocked_resources"][-1] == {
        "origin": "https://waa-pa.clients6.google.com", "resource_type": "fetch",
        "reason": "non_public_or_unresolved_host", "path_family": "other", "frame": "main",
        "method": "POST", "count": 1,
    }
    assert "private" not in str(runtime.status_base()["blocked_resources"])


@pytest.mark.asyncio
async def test_gemini_auth_document_and_frame_dependencies_are_narrow(tmp_path, monkeypatch):
    async def public(_host):
        return True
    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    main = Frame("https://gemini.google.com/app")
    child = Frame("https://gemini.google.com/frame", main)
    runtime._page = Page("https://gemini.google.com/app", main)

    cases = (
        ("https://accounts.google.com/ServiceLogin?account=secret", "document", "GET", True, child, "continued"),
        ("https://accounts.google.com/RotateCookies?token=secret", "document", "HEAD", True, child, "continued"),
        ("https://ogs.google.com/u/2/widget/app?user=secret", "document", "GET", True, child, "continued"),
        ("https://ogs.google.com/u/2/widget/account?user=secret", "document", "GET", True, child, "blockedbyclient"),
        ("https://accounts.google.com/ServiceLogin", "document", "POST", True, child, "blockedbyclient"),
        ("https://accounts.google.com/ServiceLogin", "document", "GET", True, main, "blockedbyclient"),
        ("https://accounts.google.com/ServiceLogin", "document", "GET", True, Frame("https://gemini.google.com/orphan"), "blockedbyclient"),
        ("https://accounts.google.com/private", "fetch", "POST", False, child, "blockedbyclient"),
    )
    for url, resource_type, method, navigation, frame, expected in cases:
        route = Route(url, resource_type=resource_type, method=method, navigation=navigation, frame=frame)
        await runtime._route_guard(route)
        assert route.result == expected

    account_frame = Frame("https://accounts.google.com/ServiceLogin", child)
    for resource_type, method, navigation, expected in (
        ("script", "GET", False, "continued"),
        ("stylesheet", "HEAD", False, "continued"),
        ("image", "GET", False, "continued"),
        ("font", "GET", False, "continued"),
        ("fetch", "POST", False, "continued"),
        ("fetch", "DELETE", False, "blockedbyclient"),
        ("script", "POST", False, "blockedbyclient"),
        ("fetch", "POST", True, "blockedbyclient"),
    ):
        route = Route("https://accounts.google.com/frame-resource?secret=value", resource_type=resource_type,
                      method=method, navigation=navigation, frame=account_frame)
        await runtime._route_guard(route)
        assert route.result == expected

    serialized = str(runtime.status_base()["blocked_resources"])
    for private in ("secret", "token=", "account=", "user=", "frame-resource"):
        assert private not in serialized


@pytest.mark.asyncio
async def test_gemini_static_resources_allow_only_exact_main_frame_reads(tmp_path, monkeypatch):
    async def public(_host):
        return True
    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://gemini.google.com"})
    main = Frame("https://gemini.google.com/app")
    child = Frame("https://gemini.google.com/frame", main)
    runtime._page = Page("https://gemini.google.com/app", main)
    cases = (
        ("https://www.google.com/static.js?secret=yes", "script", "GET", False, main, "continued"),
        ("https://lh3.googleusercontent.com/image?secret=yes", "image", "HEAD", False, main, "continued"),
        ("https://www.google.com/static.js", "script", "GET", False, child, "blockedbyclient"),
        ("https://www.google.com/static.js", "script", "POST", False, main, "blockedbyclient"),
        ("https://www.google.com/static.js", "script", "GET", True, main, "blockedbyclient"),
        ("https://google.com/static.js", "script", "GET", False, main, "blockedbyclient"),
        ("https://lh3.googleusercontent.com/image", "image", "GET", False, child, "blockedbyclient"),
    )
    for url, resource_type, method, navigation, frame, expected in cases:
        route = Route(url, resource_type=resource_type, method=method, navigation=navigation, frame=frame)
        await runtime._route_guard(route)
        assert route.result == expected
    serialized = str(runtime.status_base()["blocked_resources"])
    assert "secret" not in serialized
    assert "static.js" not in serialized and "image?" not in serialized
