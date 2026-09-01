"""Verify pinned store refs, URL validation, and failed-load recovery offline."""

# A url that names no GitHub repository is skipped everywhere, through one
# shared parse.
import threading
import time
from types import SimpleNamespace

import fixtures  # noqa: F401  (isolated --data tempdir; import first)
import globals as gl

from src.backend.Store.StoreBackend import StoreBackend
from src.backend.Store.prepare_pool import PreparePool
from src.backend.Store.store_result import StoreFetchError
from src.backend.Store.StoreURL import parse_repo_url


# Urls the store cannot turn into an owner and repository pair, and so must
# never act on. Free text, a non-GitHub host, an owner with no repository.
UNUSABLE_URLS = (
    "not a url at all",
    "https://gitlab.example.com/someone/plugin",
    "https://github.com/someone",
)


def _make_backend() -> StoreBackend:
    sb = StoreBackend.__new__(StoreBackend)  # skip __init__, which spawns a fetch thread
    from src.backend.Store.StoreCache import StoreCache
    sb.store_cache = StoreCache()
    # What __init__ would have built for the catalog fan-out.
    sb._fetch_limiter = threading.Semaphore(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb._prepare_pool = PreparePool(StoreBackend.MAX_CONCURRENT_REQUESTS)
    sb.official_authors = []
    return sb


def _fetch_fail(url):
    raise StoreFetchError(url, "offline")


def test_branch_is_the_pin_when_offline() -> None:
    """The official ref is the pin constant, decided with no fetch, so an
    offline start still yields well-formed store URLs."""
    fixtures.install_stub_globals()
    sb = _make_backend()
    sb.request_from_url = _fetch_fail

    branch = sb.get_official_store_branch()
    assert branch == StoreBackend.STORE_PIN, (
        f"the official ref must be the pin, offline or not, got {branch!r}"
    )

    stores = sb.get_stores()
    assert stores[0] == (StoreBackend.STORE_REPO_URL, StoreBackend.STORE_PIN)
    for url, b in stores:
        assert isinstance(b, str) and b, f"get_stores yielded non-str branch {b!r} for {url}"


def test_custom_store_entries_are_sanitized() -> None:
    fixtures.install_stub_globals(app_settings={
        "store": {
            "enable-custom-stores": True,
            "custom-stores": [
                {"url": "https://github.com/someone/store", "branch": None},
                {"url": None, "branch": "main"},  # must be skipped entirely
                {"url": "https://github.com/other/store", "branch": "1.5.0"},
            ],
        },
    })
    sb = _make_backend()
    sb.request_from_url = _fetch_fail

    stores = sb.get_stores()
    urls = [u for u, _ in stores]
    assert None not in urls, f"url-less custom store must be skipped, got {stores}"
    for url, b in stores:
        assert isinstance(b, str) and b, f"get_stores yielded non-str branch {b!r} for {url}"


class _Item:
    """Stands in for PluginData. process_store_data filters by data_class."""
    def __init__(self, url: str):
        self.url = url


class _EmptyCatalog:
    """A request_from_url answer holding an empty catalog file."""
    text = "[]"
    content = b"[]"


def test_catalog_survives_unparseable_custom_urls() -> None:
    """End to end through process_store_data. The unusable entries are
    skipped, the healthy ones are prepared, and nothing raises."""
    fixtures.install_stub_globals(app_settings={
        "store": {
            "enable-custom-stores": True,
            "custom-stores": [
                {"url": "not a url at all", "branch": "main"},
                {"url": "https://github.com/someone/store", "branch": "main"},
            ],
            "enable-custom-plugins": True,
            "custom-plugins": [
                {"url": "https://gitlab.example.com/someone/plugin", "branch": "main"},
                {"url": "", "branch": "main"},  # a row added but never filled in
                {"url": "https://github.com/someone/plugin", "branch": "main"},
            ],
        },
    })
    sb = _make_backend()
    sb.request_from_url = lambda url: _EmptyCatalog()

    store_urls = [url for url, _ in sb.get_stores()]
    assert "not a url at all" not in store_urls, (
        f"a custom store that names no repository must be skipped, got {store_urls}"
    )
    assert "https://github.com/someone/store" in store_urls, (
        f"the healthy custom store must survive, got {store_urls}"
    )

    plugin_urls = [url for url, _ in sb.get_custom_plugins()]
    assert plugin_urls == ["https://github.com/someone/plugin"], (
        f"only the healthy custom plugin may reach the catalog, got {plugin_urls}"
    )

    prepared: list[str] = []

    def fake_prepare(entry, include_images=True, verified=False):
        prepared.append(entry["url"])
        return _Item(entry["url"])

    results = sb.process_store_data(
        StoreBackend.PLUGIN_FILE, fake_prepare, sb.get_custom_plugins, _Item
    )
    assert results is not None, (
        "one unusable custom url must not fail the whole catalog load"
    )
    assert prepared == ["https://github.com/someone/plugin"], (
        f"the catalog prepared {prepared}"
    )
    assert [item.url for item in results] == ["https://github.com/someone/plugin"]


def test_prepare_plugin_skips_bad_url() -> None:
    """A catalog entry (not just a settings one) with an unusable url is
    dropped before anything is fetched for it."""
    sb = _make_backend()

    def explode(*args, **kwargs):
        raise AssertionError("an unusable url must be skipped before any fetch")

    sb.get_last_commit = explode
    sb.get_manifest = explode

    for url in UNUSABLE_URLS:
        result = sb.prepare_plugin({"url": url, "branch": "main"})
        assert result is None, f"prepare_plugin({url!r}) returned {result!r}"


def test_settings_row_refuses_bad_url() -> None:
    """Use the shared URL parser on a headless CustomContentEntry stand-in."""
    from src.windows.Settings.Settings import CustomContentEntry

    gl.lm = SimpleNamespace(get=lambda key, fallback=None: key)

    class FakeEntryRow:
        def __init__(self, text: str):
            self._text = text
            self.css_classes: set[str] = set()
            self.tooltip: str | None = None

        def get_text(self) -> str:
            return self._text

        def add_css_class(self, name: str) -> None:
            self.css_classes.add(name)

        def remove_css_class(self, name: str) -> None:
            self.css_classes.discard(name)

        def set_tooltip_text(self, text) -> None:
            self.tooltip = text

    class FakeRow:
        validate_and_mark_url = CustomContentEntry.validate_and_mark_url

        def __init__(self, text: str):
            self.url = FakeEntryRow(text)

    for url in UNUSABLE_URLS:
        assert parse_repo_url(url) is None, f"test data {url!r} is actually parseable"
        row = FakeRow(url)
        assert row.validate_and_mark_url() is None, (
            f"the settings row must refuse {url!r} instead of storing it"
        )
        assert "error" in row.url.css_classes, f"{url!r} must be flagged in the row"
        assert row.url.tooltip, f"{url!r} must say why it was refused"

    for text, stored in (
        ("https://github.com/someone/plugin", "https://github.com/someone/plugin"),
        ("  https://github.com/someone/plugin  ", "https://github.com/someone/plugin"),
        ("", ""),  # clearing a row must always take effect
    ):
        row = FakeRow(text)
        assert row.validate_and_mark_url() == stored, (
            f"{text!r} must be stored as {stored!r}"
        )
        assert "error" not in row.url.css_classes
        assert row.url.tooltip is None


def test_store_page_rearms_after_failed_load() -> None:
    """Drive StorePage failed-load recovery on a headless stand-in."""
    from src.windows.Store.StorePage import StorePage
    from gi.repository import GLib

    class FakePage:
        ensure_loaded = StorePage.ensure_loaded
        _load_guarded = StorePage._load_guarded
        show_connection_error = StorePage.show_connection_error

        def __init__(self):
            self._loaded = False
            self.load_calls = 0
            self.fail = True
            self.visible_child = None
            self.no_connection_page = object()

        def load(self):
            self.load_calls += 1
            if self.fail:
                raise RuntimeError("boom: simulated JSONDecodeError-style load failure")

        def set_visible_child(self, child):
            self.visible_child = child

    page = FakePage()

    # 1. A failing load must re-arm and show the error page.
    page.ensure_loaded()
    assert fixtures.wait_until(lambda: page.load_calls == 1 and not page._loaded), (
        "failed load must reset _loaded so the tab can retry"
    )
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + 3.0
    while page.visible_child is None and time.monotonic() < deadline:
        ctx.iteration(False)
    assert page.visible_child is page.no_connection_page, (
        "failed load must land on the error page"
    )

    # 2. After the failure, ensure_loaded must load again.
    page.fail = False
    page.ensure_loaded()
    assert fixtures.wait_until(lambda: page.load_calls == 2), (
        "revisiting the tab after a failure must retry the load"
    )
    assert page._loaded is True

    # 3. A loaded tab stays a no-op.
    page.ensure_loaded()
    time.sleep(0.1)
    assert page.load_calls == 2, "an already-loaded tab must not reload"


def main() -> None:
    fixtures.start_watchdog(30, label="scenario_store_branch_contract")
    test_branch_is_the_pin_when_offline()
    test_custom_store_entries_are_sanitized()
    test_catalog_survives_unparseable_custom_urls()
    test_prepare_plugin_skips_bad_url()
    test_settings_row_refuses_bad_url()
    test_store_page_rearms_after_failed_load()
    print("scenario_store_branch_contract: PASS")


if __name__ == "__main__":
    main()
