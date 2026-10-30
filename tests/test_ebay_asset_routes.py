import pytest

import worker.app as worker_app
from worker.app import BrowserRuntime


class Frame:
    def __init__(self, url, parent=None):
        self.url = url
        self.parent_frame = parent


class Page:
    def __init__(self, url, main_frame):
        self.url = url
        self.main_frame = main_frame


class Request:
    def __init__(self, url, resource_type, method="GET", navigation=False, frame=None):
        self.url = url
        self.resource_type = resource_type
        self.method = method
        self.navigation = navigation
        self.frame = frame

    def is_navigation_request(self):
        return self.navigation


class Route:
    def __init__(self, url, resource_type, method="GET", navigation=False, frame=None):
        self.request = Request(url, resource_type, method, navigation, frame)
        self.result = None

    async def continue_(self):
        self.result = "continued"

    async def abort(self, reason):
        self.result = reason


@pytest.mark.asyncio
async def test_only_observed_main_frame_ebay_assets_are_allowed(tmp_path, monkeypatch):
    async def public(host):
        return host in {"www.ebay.com", "ir.ebaystatic.com", "i.ebayimg.com"}

    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://www.ebay.com"})
    main = Frame("https://www.ebay.com/")
    runtime._page = Page("https://www.ebay.com/", main)

    for host, resource_type in (("ir.ebaystatic.com", "stylesheet"),
                                ("ir.ebaystatic.com", "script"),
                                ("i.ebayimg.com", "image")):
        for method in ("GET", "HEAD"):
            route = Route(f"https://{host}/asset", resource_type, method, frame=main)
            await runtime._route_guard(route)
            assert route.result == "continued"


@pytest.mark.asyncio
async def test_ebay_asset_exemption_rejects_scope_type_method_navigation_and_frame(tmp_path, monkeypatch):
    async def public(_host):
        return True

    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://www.ebay.com"})
    main = Frame("https://www.ebay.com/")
    child = Frame("https://www.ebay.com/frame", main)
    runtime._page = Page("https://www.ebay.com/", main)

    denied = (
        ("https://cdn.ebaystatic.com/asset", "script", "GET", False, main),
        ("https://ir.ebaystatic.com:8443/asset", "script", "GET", False, main),
        ("https://ir.ebaystatic.com/asset", "image", "GET", False, main),
        ("https://ir.ebaystatic.com/asset", "document", "GET", True, main),
        ("https://ir.ebaystatic.com/asset", "script", "POST", False, main),
        ("https://ir.ebaystatic.com/asset", "script", "OPTIONS", False, main),
        ("https://ir.ebaystatic.com/asset", "script", "GET", True, main),
        ("https://ir.ebaystatic.com/asset", "script", "GET", False, child),
    )
    for url, kind, method, navigation, frame in denied:
        route = Route(url, kind, method, navigation, frame)
        await runtime._route_guard(route)
        assert route.result == "blockedbyclient"

    runtime._route_origins = frozenset({"https://other.example"})
    route = Route("https://ir.ebaystatic.com/asset", "script", frame=main)
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"

    runtime._route_origins = frozenset({"https://www.ebay.com"})
    runtime._page = Page("https://other.example/", Frame("https://other.example/"))
    route = Route("https://i.ebayimg.com/asset", "image", frame=runtime._page.main_frame)
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"


@pytest.mark.asyncio
async def test_ebay_assets_still_obey_public_host_and_egress_route_guards(tmp_path, monkeypatch):
    async def private(_host):
        return False

    monkeypatch.setattr(worker_app, "_public_host", private)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://www.ebay.com"})
    main = Frame("https://www.ebay.com/")
    runtime._page = Page("https://www.ebay.com/", main)
    private_route = Route("https://ir.ebaystatic.com/asset", "script", frame=main)
    await runtime._route_guard(private_route)
    assert private_route.result == "blockedbyclient"
    assert runtime.status_base()["blocked_resources"][-1]["reason"] == "non_public_or_unresolved_host"

    async def public(_host):
        return True

    monkeypatch.setattr(worker_app, "_public_host", public)
    runtime.egress_ready = False
    no_egress = Route("https://i.ebayimg.com/asset", "image", frame=main)
    await runtime._route_guard(no_egress)
    assert no_egress.result == "blockedbyclient"
    assert runtime.status_base()["blocked_resources"][-1]["reason"] == "egress_unavailable"

    runtime.egress_ready = True
    runtime.route_generation = 0
    no_route = Route("https://i.ebayimg.com/asset", "image", frame=main)
    await runtime._route_guard(no_route)
    assert no_route.result == "blockedbyclient"
    assert runtime.status_base()["blocked_resources"][-1]["reason"] == "egress_unavailable"
