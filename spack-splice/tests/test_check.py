"""Tests for the recipe-constraint check.

The check is deliberately shallow: it asks whether each dependency in the graph
satisfies the constraint written next to it, and nothing that would require search.
These tests pin that boundary as much as the behaviour.
"""

import os

try:
    from spack.extensions.splice import check
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import check


class FakeNode:
    def __init__(self, name, ok=True):
        self.name = name
        self._ok = ok

    def satisfies(self, _constraint):
        return self._ok

    def format(self, _fmt):
        return f"{self.name}@1.0"


class FakeEdge:
    def __init__(self, node):
        self.spec = node


class FakeSpec:
    def __init__(self, name="pkg", deps=(), conflicts_hold=False):
        self.name = name
        self._edges = [FakeEdge(d) for d in deps]
        self._conflicts_hold = conflicts_hold

    def satisfies(self, when):
        # "" is the unconditional when-key; anything else is a conflict probe.
        return True if when == "" else self._conflicts_hold

    def edges_to_dependencies(self, name=None, depflag=None):
        return self._edges


def _pkg(deps=(), conflicts=()):
    cls = type("P", (), {})
    cls.dependencies = {"": {n: type("D", (), {"spec": f"{n}@1:"})() for n in deps}}
    cls.conflicts = {"": list(conflicts)}
    return cls


def _patch(monkey, pkg, virtuals=()):
    """Swap in a fake repo so the check does not need a real package."""
    import spack.repo

    saved = spack.repo.PATH
    spack.repo.PATH = type(
        "R",
        (),
        {
            "get_pkg_class": staticmethod(lambda _n: pkg),
            "is_virtual": staticmethod(lambda n: n in virtuals),
        },
    )()
    try:
        return monkey()
    finally:
        spack.repo.PATH = saved


def test_satisfied_constraints_produce_no_problems():
    spec = FakeSpec(deps=[FakeNode("cmake", ok=True)])
    got = _patch(lambda: check.check_spec(spec), _pkg(deps=["cmake"]))
    assert got == []


def test_unsatisfied_version_constraint_is_reported():
    spec = FakeSpec(deps=[FakeNode("cetmodules", ok=False)])
    (problem,) = _patch(lambda: check.check_spec(spec), _pkg(deps=["cetmodules"]))
    assert problem.package == "pkg"
    assert "requires" in problem.detail and "cetmodules" in problem.detail


def test_missing_dependency_is_reported():
    spec = FakeSpec(deps=[])
    (problem,) = _patch(lambda: check.check_spec(spec), _pkg(deps=["zlib"]))
    assert "no zlib dependency" in problem.detail


def test_virtuals_are_skipped():
    """Which node provides a virtual is a search question, so out of scope."""
    spec = FakeSpec(deps=[])
    got = _patch(lambda: check.check_spec(spec), _pkg(deps=["cxx"]), virtuals={"cxx"})
    assert got == []


def test_active_conflict_is_reported():
    spec = FakeSpec(deps=[], conflicts_hold=True)
    problems = _patch(
        lambda: check.check_spec(spec), _pkg(conflicts=[("+broken", "does not build")])
    )
    assert any("conflicts with" in p.detail and "does not build" in p.detail for p in problems)


def test_inactive_conflict_is_ignored():
    spec = FakeSpec(deps=[], conflicts_hold=False)
    got = _patch(lambda: check.check_spec(spec), _pkg(conflicts=[("+broken", None)]))
    assert got == []


def test_missing_recipe_is_a_problem_not_a_crash():
    class Boom:
        @staticmethod
        def get_pkg_class(_n):
            raise KeyError("nope")

        @staticmethod
        def is_virtual(_n):
            return False

    import spack.repo

    saved = spack.repo.PATH
    spack.repo.PATH = Boom()
    try:
        (problem,) = check.check_spec(FakeSpec())
    finally:
        spack.repo.PATH = saved
    assert "no recipe found" in problem.detail


def test_problem_renders_readably():
    assert str(check.Problem("art", "requires 'x'")) == "art: requires 'x'"


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
