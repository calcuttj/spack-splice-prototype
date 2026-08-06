"""Tests for the ABI-preservation checks in specbuild.

The spec-construction itself needs real Spec internals and a store, so it is covered
by the M3 acceptance run in the README rather than here. What is worth unit-testing
is the guarantee those functions exist to protect: that compiler and runtime nodes
come through a splice with their identity intact.
"""

try:
    from spack.extensions.splice import specbuild
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import os

    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import specbuild


class FakeSpec:
    def __init__(self, name, dag, deps=()):
        self.name = name
        self._dag = dag
        self._deps = list(deps)

    def dag_hash(self):
        return self._dag

    def traverse(self, **kwargs):
        seen, out, stack = set(), [], [self]
        while stack:
            n = stack.pop()
            if n.name in seen:
                continue
            seen.add(n.name)
            out.append(n)
            stack.extend(n._deps)
        return out


def _stack(runtime_hash="rt1", libc_hash="libc1"):
    return FakeSpec(
        "app",
        "app1",
        [
            FakeSpec("gcc-runtime", runtime_hash),
            FakeSpec("glibc", libc_hash),
            FakeSpec("some-lib", "lib1"),
        ],
    )


def test_abi_nodes_picks_out_compiler_and_runtime():
    found = specbuild.abi_nodes(_stack())
    assert found == {"gcc-runtime": "rt1", "glibc": "libc1"}
    assert "some-lib" not in found


def test_abi_preserved_when_runtime_nodes_are_identical():
    assert specbuild.check_abi_preserved(_stack(), _stack()) == []


def test_abi_drift_detected_when_runtime_changes():
    """The whole ABI guarantee is that this never happens."""
    assert specbuild.check_abi_preserved(_stack(), _stack(runtime_hash="rt2")) == [
        "gcc-runtime"
    ]


def test_abi_drift_reports_every_changed_node():
    drift = specbuild.check_abi_preserved(
        _stack(), _stack(runtime_hash="rt2", libc_hash="libc2")
    )
    assert drift == ["gcc-runtime", "glibc"]


def test_dev_nodes_selects_by_name():
    found = specbuild.dev_nodes(_stack(), ["some-lib"])
    assert list(found) == ["some-lib"]


class FakeEdge:
    def __init__(self, name, dag):
        self.spec = type("S", (), {"name": name, "dag_hash": lambda self=None, d=dag: d})()


class FakeBuildable:
    def __init__(self, name, deps):
        self.name = name
        self._deps = deps

    def edges_to_dependencies(self, name=None, depflag=None):
        return [FakeEdge(n, f"h-{n}") for n in self._deps]


def test_frontier_excludes_other_dev_packages():
    """The frontier is what gets symlinked into the view; a dev package is built,
    not linked, so it must not appear there."""
    buildable = {
        "cetlib": FakeBuildable("cetlib", ["cetlib-except", "boost"]),
        "cetlib-except": FakeBuildable("cetlib-except", ["boost"]),
    }
    assert specbuild.frontier_of(buildable) == {"h-boost"}


def test_frontier_is_read_from_the_reconciled_specs():
    """Recipe drift can add a dependency that exists only on the dev spec, so the
    frontier cannot come from the base DAG."""
    buildable = {"pkg": FakeBuildable("pkg", ["newly-added"])}
    assert specbuild.frontier_of(buildable) == {"h-newly-added"}


def _linked(parent_name, child_name):
    """Two real Specs joined by one real edge.

    Deliberately not a fake. ``remove_dependency`` exists to mutate Spack's
    ``_EdgeMap``, and a fake dict-of-lists satisfies dict semantics that the real
    class does not -- which is exactly how the deletion bug below survived: every
    test passed while a two-package dev set could not be reported at all.
    """
    import spack.deptypes as dt
    import spack.spec

    parent, child = spack.spec.Spec(parent_name), spack.spec.Spec(child_name)
    parent.add_dependency_edge(child, depflag=dt.BUILD | dt.LINK, virtuals=(), direct=True)
    return parent, child


def test_remove_dependency_drops_the_edge_both_ways():
    parent, child = _linked("cetlib", "cetlib-except")
    removed = specbuild.remove_dependency(parent, "cetlib-except")

    assert len(removed) == 1
    assert not parent.edges_to_dependencies(name="cetlib-except")
    assert not child.edges_from_dependents(name="cetlib")


def test_remove_dependency_leaves_no_empty_keys():
    """``_EdgeMap.add`` never creates an empty list, so neither may we: a name in
    the map with no edges is a shape the rest of Spack never produces."""
    parent, child = _linked("cetlib", "cetlib-except")
    specbuild.remove_dependency(parent, "cetlib-except")

    assert "cetlib-except" not in parent._dependencies
    assert "cetlib" not in child._dependents
    assert len(parent._dependencies) == 0


def test_remove_dependency_keeps_other_edges():
    parent, child = _linked("cetlib", "cetlib-except")
    import spack.deptypes as dt
    import spack.spec

    other = spack.spec.Spec("boost")
    parent.add_dependency_edge(other, depflag=dt.LINK, virtuals=(), direct=True)

    specbuild.remove_dependency(parent, "cetlib-except")
    assert [e.spec.name for e in parent.edges_to_dependencies()] == ["boost"]


def test_replace_dependency_repoints_at_the_dev_build():
    """The whole point: a dev package depending on another dev package must end up
    pointing at the dev build. This is the path that raised ``TypeError: '_EdgeMap'
    object does not support item deletion`` on any dev set larger than one."""
    import spack.spec

    parent, installed = _linked("cetlib", "cetlib-except")
    replacement = spack.spec.Spec("cetlib-except")

    specbuild.replace_dependency(parent, "cetlib-except", replacement)

    edges = parent.edges_to_dependencies(name="cetlib-except")
    assert len(edges) == 1
    assert edges[0].spec is replacement
    assert not installed.edges_from_dependents(name="cetlib")


def test_replace_dependency_preserves_the_edge_attributes():
    """A re-pointed edge must keep its deptypes, or the rebuilt spec quietly stops
    being a link dependency and the dev build is never linked against."""
    import spack.spec

    parent, _ = _linked("cetlib", "cetlib-except")
    original = parent.edges_to_dependencies(name="cetlib-except")[0]
    depflag, virtuals, direct = original.depflag, original.virtuals, original.direct

    specbuild.replace_dependency(parent, "cetlib-except", spack.spec.Spec("cetlib-except"))

    new = parent.edges_to_dependencies(name="cetlib-except")[0]
    assert (new.depflag, new.virtuals, new.direct) == (depflag, virtuals, direct)


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
