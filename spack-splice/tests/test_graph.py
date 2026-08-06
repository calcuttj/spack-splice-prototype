"""Tests for the interval-closure graph algebra.

These use hand-built fake DAGs rather than real specs, so they need neither a store
nor pytest -- run them under ``spack unit-test --extension=splice``, under plain
pytest, or directly with ``spack python tests/test_graph.py``.

The one thing they cannot cover is the shared-parent-pointer trap described in
``graph.py``; that needs a real database and is covered by the acceptance check in
the README.
"""

try:
    from spack.extensions.splice import graph
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import os

    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import graph


class FakeSpec:
    """Minimal stand-in for a concrete Spec: a name and outgoing edges."""

    def __init__(self, name):
        self.name = name
        self.version = "1.0"
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


class FakeEdge:
    def __init__(self, spec):
        self.spec = spec


def chain():
    """A -> B -> C -> D, the example from the design discussion."""
    a, b, c, d = (FakeSpec(n) for n in "ABCD")
    a.depends_on(b)
    b.depends_on(c)
    c.depends_on(d)
    return a, b, c, d


def test_interval_closure_pulls_in_the_middle():
    """Picking A and C must also rebuild B, which sits between them."""
    a, _, _, _ = chain()
    assert graph.dev_set(a, ["A", "C"]) == {"A", "B", "C"}


def test_single_pick_rebuilds_only_itself():
    """One pick has no interval, so nothing else is implied."""
    a, _, _, _ = chain()
    assert graph.dev_set(a, ["C"]) == {"C"}


def test_nothing_below_the_lowest_pick():
    """D is under the dev set and must stay installed, not be rebuilt."""
    a, _, _, _ = chain()
    assert "D" not in graph.dev_set(a, ["A", "C"])


def test_frontier_is_the_boundary():
    a, _, _, _ = chain()
    assert graph.frontier(a, {"A", "B", "C"}) == {"D"}


def test_diamond_includes_both_arms():
    """A depends on B and C, both of which depend on D. Picking A and D takes all."""
    a, b, c, d = (FakeSpec(n) for n in "ABCD")
    a.depends_on(b, c)
    b.depends_on(d)
    c.depends_on(d)
    assert graph.dev_set(a, ["A", "D"]) == {"A", "B", "C", "D"}


def test_disjoint_picks_do_not_bridge():
    """Two picks on unrelated branches imply nothing between them."""
    root, x, y = FakeSpec("root"), FakeSpec("X"), FakeSpec("Y")
    root.depends_on(x, y)
    assert graph.dev_set(root, ["X", "Y"]) == {"X", "Y"}


def test_build_order_is_dependency_first():
    """A dev package must be spliced and built before anything that depends on it."""
    a, _, _, _ = chain()
    order = graph.build_order(a, {"A", "B", "C"})
    assert order.index("C") < order.index("B") < order.index("A")


def test_build_order_covers_exactly_the_dev_set():
    a, _, _, _ = chain()
    assert set(graph.build_order(a, {"A", "C"})) == {"A", "C"}


def test_build_order_handles_diamonds_without_duplicates():
    a, b, c, d = (FakeSpec(n) for n in "ABCD")
    a.depends_on(b, c)
    b.depends_on(d)
    c.depends_on(d)
    order = graph.build_order(a, {"A", "B", "C", "D"})
    assert len(order) == len(set(order)) == 4
    assert order.index("D") < order.index("B") < order.index("A")
    assert order.index("D") < order.index("C") < order.index("A")


def test_unshadowable_reports_only_dependents_outside_the_dev_set():
    a, _, _, _ = chain()
    # Rebuilding only C: B links it directly and cannot be shadowed.
    assert graph.unshadowable(a, {"C"}) == {"C": {"B"}}
    # Rebuilding B and C: nothing outside the set links C any more.
    assert "C" not in graph.unshadowable(a, {"B", "C"})


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
