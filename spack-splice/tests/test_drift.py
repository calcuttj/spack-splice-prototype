"""Tests for reconciling a copied spec against an edited recipe.

Real reconciliation needs a package class and a store, so that is covered by the M6
acceptance run. What is unit-testable is the decision logic: which variants and
dependencies count as drift, and -- more importantly -- which must never be touched.
"""

import os

try:
    from spack.extensions.splice import drift
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import drift


class FakeVariant:
    def __init__(self, name, default):
        self.name = name
        self.default = default

    def make_default(self):
        return f"{self.name}={self.default}"


class FakeEdge:
    def __init__(self, name, virtuals=()):
        self.spec = type("S", (), {"name": name})()
        self.virtuals = tuple(virtuals)
        self.depflag = 1


class FakeSpec:
    """Only what the reconciler touches: variants and outgoing edges."""

    def __init__(self, variants=(), deps=()):
        self.name = "pkg"
        self.variants = dict.fromkeys(variants, "v")
        self._edges = list(deps)
        self.architecture = "linux-debian12-x86_64_v3"

    def satisfies(self, _when):
        return True

    def edges_to_dependencies(self, name=None, depflag=None):
        return [e for e in self._edges if name is None or e.spec.name == name]

    def traverse(self, **kwargs):
        return [self]

    def dag_hash(self):
        return "pkg"


class FakePkg:
    def __init__(self, variants=(), deps=()):
        self._variants = {v.name: v for v in variants}
        # {when: {name: Dependency-ish}}
        self.dependencies = {"": {n: type("D", (), {"depflag": 1})() for n in deps}}

    def variant_names(self):
        return list(self._variants)

    def variant_definitions(self, name):
        return [("", self._variants[name])]


def test_new_recipe_variant_is_added_at_its_default():
    spec = FakeSpec()
    added, removed = drift._reconcile_variants(spec, FakePkg([FakeVariant("shiny", False)]))
    assert added == {"shiny": "shiny=False"}
    assert removed == []
    assert "shiny" in spec.variants


def test_variant_dropped_from_the_recipe_is_removed():
    spec = FakeSpec(variants=["gone"])
    _, removed = drift._reconcile_variants(spec, FakePkg())
    assert removed == ["gone"]
    assert "gone" not in spec.variants


def test_spack_managed_variants_are_never_removed():
    """dev_path is splice's own marker; losing it would un-develop the package."""
    spec = FakeSpec(variants=["dev_path", "patches", "commit"])
    _, removed = drift._reconcile_variants(spec, FakePkg())
    assert removed == []
    assert "dev_path" in spec.variants


def test_existing_variant_is_left_alone():
    spec = FakeSpec(variants=["cxxstd"])
    spec.variants["cxxstd"] = "cxxstd=17"
    added, removed = drift._reconcile_variants(spec, FakePkg([FakeVariant("cxxstd", "20")]))
    assert added == {} and removed == []
    assert spec.variants["cxxstd"] == "cxxstd=17", "must not reset a chosen value"


def test_wanted_dependencies_reads_the_recipe():
    assert set(drift._wanted_dependencies(FakeSpec(), FakePkg(deps=["zlib", "cmake"]))) == {
        "zlib",
        "cmake",
    }


def test_unsatisfiable_when_is_treated_as_not_applying():
    """A `when=` may reference a variant the spec lacks -- the very thing we're
    fixing -- so evaluation must not raise."""

    class Boom(FakeSpec):
        def satisfies(self, _when):
            raise ValueError("no such variant")

    assert drift._satisfies(Boom(), "+whatever") is False


def test_compiler_nodes_are_shielded_from_removal():
    """gcc and friends are injected by concretization, never named in depends_on.

    Treating their absence from the recipe as "the user deleted this" would strip
    the compiler out of every dev spec.
    """
    for name in ("gcc", "gcc-runtime", "glibc", "compiler-wrapper"):
        assert name in drift.ABI_NODES


def test_drift_is_falsy_when_nothing_changed():
    assert not drift.Drift("pkg", {}, [], {}, [])


def test_drift_summary_lists_every_kind_of_change():
    text = drift.Drift("pkg", {"a": "1"}, ["b"], {"c": "base spec"}, ["d"]).summary()
    for fragment in ("pkg:", "+variants a=1", "-variants b", "+deps c(base spec)", "-deps d"):
        assert fragment in text


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
