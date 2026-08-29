"""Tests for the shadowability check.

Whether a dev build can displace an installed one comes down to two things: the ELF
tags of the packages above it, and whether those packages are being developed too.
Both are faked here -- the readelf call is stubbed, so these need neither a store nor
binaries on disk.
"""

import os

try:
    from spack.extensions.splice import build, graph, shadow
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import build, graph, shadow


class FakeSpec:
    """Minimal stand-in for a concrete Spec: a name, a prefix and outgoing edges."""

    def __init__(self, name):
        self.name = name
        self.version = "1.0"
        self.prefix = f"/prefix/{name}"
        self._deps = []

    def dag_hash(self):
        return self.name

    def depends_on(self, *others):
        self._deps.extend(others)
        return self

    def edges_to_dependencies(self, depflag=None):
        return [FakeEdge(d) for d in self._deps]

    def traverse(self, deptype=None, order="pre", key=None, root=True):
        seen, out, stack = set(), [], [self]
        while stack:
            node = stack.pop()
            if node.name in seen:
                continue
            seen.add(node.name)
            out.append(node)
            stack.extend(node._deps)
        return out

    def format(self, _fmt):
        return f"{self.name}@{self.version}"


class FakeEdge:
    def __init__(self, spec):
        self.spec = spec


class _Elf:
    """Pretends each package's binaries carry the tags given by name."""

    def __init__(self, tags_by_name):
        self._saved = (shadow.binaries_of, build.linking_type_of)
        # One file per package, named for the package, so linking_type_of can tell
        # which package it is being asked about.
        shadow.binaries_of = lambda prefix, limit=None: (
            [f"{prefix}/lib/lib.so"] if os.path.basename(str(prefix)) in tags_by_name else []
        )
        build.linking_type_of = lambda path: tags_by_name[path.split("/")[2]]

    def restore(self):
        shadow.binaries_of, build.linking_type_of = self._saved


def _blocked(tags_by_name, root, package, developed=()):
    elf = _Elf(tags_by_name)
    try:
        return [(s.name, v) for s, v in shadow.blocked_dependents(root, package, developed)]
    finally:
        elf.restore()


# -- reading the tags ------------------------------------------------------


def _shadowable(tags):
    elf = _Elf({"x": tags})
    try:
        return shadow.shadowable(FakeSpec("x"))
    finally:
        elf.restore()


def test_runpath_is_shadowable():
    assert _shadowable({"RUNPATH"}) is True


def test_rpath_alone_is_not_shadowable():
    """DT_RPATH is consulted before LD_LIBRARY_PATH, so nothing can redirect it."""
    assert _shadowable({"RPATH"}) is False


def test_both_tags_is_shadowable():
    """When both are present the loader honours RUNPATH, so redirection works."""
    assert _shadowable({"RPATH", "RUNPATH"}) is True


def test_no_rpath_at_all_is_shadowable():
    assert _shadowable(set()) is True


def test_unreadable_is_undetermined():
    """No readelf, or not an ELF file: say so rather than guessing either way."""
    assert _shadowable(None) is None


def test_nothing_to_inspect_is_undetermined():
    elf = _Elf({})
    try:
        assert shadow.shadowable(FakeSpec("empty")) is None
    finally:
        elf.restore()


# -- which dependents are blocked -----------------------------------------


def _chain():
    """app -> mid -> leaf, all linked."""
    leaf = FakeSpec("leaf")
    mid = FakeSpec("mid").depends_on(leaf)
    app = FakeSpec("app").depends_on(mid)
    return app, mid, leaf


def test_runpath_dependent_is_not_blocked():
    app, _mid, _leaf = _chain()
    tags = {"app": {"RUNPATH"}, "mid": {"RUNPATH"}, "leaf": {"RUNPATH"}}
    assert _blocked(tags, app, "leaf") == []


def test_rpath_dependent_is_blocked():
    app, _mid, _leaf = _chain()
    tags = {"app": {"RUNPATH"}, "mid": {"RPATH"}, "leaf": {"RUNPATH"}}
    assert _blocked(tags, app, "leaf") == [("mid", False)]


def test_a_developed_dependent_is_not_blocked():
    """It gets rebuilt against the dev package, so its rpath stops mattering."""
    app, _mid, _leaf = _chain()
    tags = {"app": {"RUNPATH"}, "mid": {"RPATH"}, "leaf": {"RUNPATH"}}
    assert _blocked(tags, app, "leaf", developed=["mid"]) == []


def test_only_direct_dependents_are_considered():
    """'app' reaches leaf through mid, so mid being fine is enough for app."""
    app, _mid, _leaf = _chain()
    tags = {"app": {"RPATH"}, "mid": {"RUNPATH"}, "leaf": {"RUNPATH"}}
    assert _blocked(tags, app, "leaf") == []


def test_undetermined_dependent_is_reported():
    """Not knowing is not the same as being fine -- surface it either way."""
    app, _mid, _leaf = _chain()
    tags = {"app": {"RUNPATH"}, "mid": None, "leaf": {"RUNPATH"}}
    assert _blocked(tags, app, "leaf") == [("mid", None)]


def test_a_root_has_nothing_above_it():
    app, _mid, _leaf = _chain()
    tags = {"app": {"RPATH"}, "mid": {"RPATH"}, "leaf": {"RPATH"}}
    assert _blocked(tags, app, "app") == []


def test_every_direct_dependent_is_checked():
    leaf = FakeSpec("leaf")
    one = FakeSpec("one").depends_on(leaf)
    two = FakeSpec("two").depends_on(leaf)
    app = FakeSpec("app").depends_on(one, two)
    tags = {"app": {"RUNPATH"}, "one": {"RPATH"}, "two": {"RPATH"}, "leaf": {"RUNPATH"}}
    assert sorted(_blocked(tags, app, "leaf")) == [("one", False), ("two", False)]


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
