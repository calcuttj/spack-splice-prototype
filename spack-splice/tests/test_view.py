"""Tests for dev_view spec filtering and content hashing.

Like ``test_graph``, these avoid needing a store by using fake specs, and run under
pytest or standalone via ``spack python tests/test_view.py``.
"""

try:
    from spack.extensions.splice import view
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import os

    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import view


class FakeSpec:
    def __init__(self, name, version="1.0", installed=True, external=False, dag="h"):
        self.name = name
        self.version = version
        self.installed = installed
        self.external = external
        self._dag = dag

    def dag_hash(self):
        return self._dag


def test_linkable_drops_externals_and_uninstalled():
    specs = [
        FakeSpec("good", dag="a"),
        FakeSpec("ext", dag="b", external=True),
        FakeSpec("nope", dag="c", installed=False),
    ]
    keep, missing = view.linkable(specs)
    assert [s.name for s in keep] == ["good"]
    assert [s.name for s in missing] == ["nope"]


def test_linkable_reports_uninstalled_separately_from_externals():
    """An external is silently skipped; an uninstalled spec is worth telling about."""
    _, missing = view.linkable([FakeSpec("ext", dag="b", external=True)])
    assert missing == []


def test_content_hash_is_order_independent():
    """The same set of specs must hash the same however it is ordered."""
    a, b = FakeSpec("a", dag="aaa"), FakeSpec("b", dag="bbb")
    assert view._content_hash([a, b]) == view._content_hash([b, a])


def test_content_hash_changes_with_contents():
    a, b = FakeSpec("a", dag="aaa"), FakeSpec("b", dag="bbb")
    assert view._content_hash([a]) != view._content_hash([a, b])


def test_installed_hash_of_missing_view_is_none():
    assert view._installed_hash("/nonexistent/path/for/splice/test") is None


def main():
    """Standalone runner, for environments without pytest."""
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001 -- a test runner should catch everything
            failures.append(name)
            print(f"FAIL  {name}: {e!r}")
    print(f"\n{len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
