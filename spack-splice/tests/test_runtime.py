"""Tests for runtime environment assembly.

Composing the full environment needs a real installed stack, so that is covered by
the M5 acceptance run. What is unit-testable is the prefix bookkeeping and the
reporting that tells a user which dev build is actually in front.
"""

import os
import tempfile

try:
    from spack.extensions.splice import runtime
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import runtime


def test_built_keeps_only_existing_prefixes():
    """An unbuilt dev package must not enter the environment."""
    with tempfile.TemporaryDirectory() as tmp:
        real = os.path.join(tmp, "built")
        os.mkdir(real)
        got = runtime.built({"here": real, "gone": os.path.join(tmp, "nope")})
        assert list(got) == ["here"]


def test_built_of_nothing_is_empty():
    assert runtime.built({}) == {}


def test_ld_library_path_is_the_required_floor():
    """Everything else is derived from the recipes, but this one cannot be.

    Spack resolves libraries via RPATH and so never populates LD_LIBRARY_PATH --
    not through prefix inspections, not through the FNAL recipes. Splice's shadowing
    depends on it entirely, so it must supply it. Deriving purely from the recipes
    left a built cetlib-except out of LD_LIBRARY_PATH and silently broke shadowing.
    """
    assert "LD_LIBRARY_PATH" in runtime.REQUIRED_PATH_VARS
    assert runtime.REQUIRED_PATH_VARS["LD_LIBRARY_PATH"] == ("lib", "lib64")


def test_recipe_derived_vars_are_not_hardcoded():
    """FHICL_FILE_PATH, PYTHONPATH, PERL5LIB and friends come from the packages.

    A hardcoded table cannot keep up: FHICL_FILE_PATH is 'fcl' for art/DUNE but
    'job' for LArSoft, and FW_SEARCH_PATH spans gdml, config_data, compatibility,
    scripts and G4.
    """
    for var in ("FHICL_FILE_PATH", "PYTHONPATH", "PERL5LIB", "CET_PLUGIN_PATH"):
        assert var not in runtime.REQUIRED_PATH_VARS


def test_summary_attributes_the_leading_path_to_its_package():
    env = runtime.EnvironmentModifications()
    env.prepend_path("LD_LIBRARY_PATH", "/dev/area/pkg-abc/lib")
    rows = runtime.summary(env, {"pkg": "/dev/area/pkg-abc"}, keys=("LD_LIBRARY_PATH",))
    (key, first, owner) = rows[0]
    assert key == "LD_LIBRARY_PATH"
    assert first == "/dev/area/pkg-abc/lib"
    assert owner == "pkg"


def test_summary_reports_unset_variables():
    env = runtime.EnvironmentModifications()
    rows = runtime.summary(env, {}, keys=("SPLICE_NOT_A_REAL_VAR",))
    assert rows[0][1] == "(unset)"


def test_summary_leaves_owner_none_for_non_dev_paths():
    env = runtime.EnvironmentModifications()
    env.prepend_path("LD_LIBRARY_PATH", "/opt/spack/installed/lib")
    rows = runtime.summary(env, {"pkg": "/dev/area/pkg-abc"}, keys=("LD_LIBRARY_PATH",))
    assert rows[0][2] is None


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
