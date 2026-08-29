"""Tests for the pre-build sanity checks.

The build itself needs a real toolchain, so it is covered by the M4 acceptance run
rather than here. What is worth unit-testing is the check that turns an otherwise
baffling cmake failure into a clear message.
"""

try:
    from spack.extensions.splice import build
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import os

    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import build

import spack.deptypes as dt


class FakeDep:
    def __init__(
        self,
        name,
        installed=True,
        external=False,
        dev=False,
        external_path=None,
        external_modules=(),
        depflag=None,
    ):
        self.name = name
        self.depflag = dt.BUILD if depflag is None else depflag
        self.installed = installed
        self.external = external
        self.external_path = external_path
        self.external_modules = external_modules
        self.variants = {"dev_path": "/src/" + name} if dev else {}
        # Nothing on disk, so check_buildable's prefix fallback treats it as absent.
        self.prefix = "/nonexistent/splice-test/" + name

    def format(self, _fmt):
        return self.name


class FakeEdge:
    """Build-only unless the dep says otherwise -- check_buildable only probes
    externals reached by a pure build edge."""

    def __init__(self, spec):
        self.spec = spec
        self.depflag = getattr(spec, "depflag", dt.BUILD)


class FakeSpec:
    """Minimal spec: which virtuals its deps provide, plus its build edges."""

    def __init__(self, name="pkg", virtuals=("c", "cxx"), build_deps=()):
        self.name = name
        self._virtuals = set(virtuals)
        self._build = list(build_deps)
        self.variants = {}

    def dependencies(self, name=None, virtuals=None):
        if virtuals:
            v = {virtuals} if isinstance(virtuals, str) else set(virtuals)
            return ["compiler"] if v & self._virtuals else []
        return [d for d in self._build if d.name == name]

    def edges_to_dependencies(self, name=None, depflag=None):
        return [FakeEdge(d) for d in self._build]


def _raises(fn):
    try:
        fn()
    except build.BuildError as e:
        return str(e)
    return None


def test_complete_spec_passes():
    spec = FakeSpec(build_deps=[FakeDep("ninja"), FakeDep("cmake")])
    assert _raises(lambda: build.check_buildable(spec)) is None


def test_missing_compiler_is_reported():
    msg = _raises(lambda: build.check_buildable(FakeSpec(virtuals=())))
    assert msg and "compiler" in msg


def test_garbage_collected_build_deps_are_reported():
    """'spack gc' prunes build-only deps; the spec still references them."""
    spec = FakeSpec(build_deps=[FakeDep("ninja", installed=False), FakeDep("cmake")])
    msg = _raises(lambda: build.check_buildable(spec))
    assert msg and "ninja" in msg and "not installed" in msg
    assert "cmake" not in msg, "installed deps should not be listed as missing"


def _with_patterns(patterns, fn, path=""):
    """Pin the executable regexes and PATH.

    The regexes come from a package recipe in real use, so pinning them keeps
    these tests independent of a package repo. PATH is pinned too, so that the
    one test asserting PATH is *not* consulted says so on any host.
    """
    import os

    original, original_path = build._executable_patterns, os.environ.get("PATH", "")
    build._executable_patterns = lambda _spec: patterns
    os.environ["PATH"] = path
    try:
        return fn()
    finally:
        build._executable_patterns = original
        os.environ["PATH"] = original_path


def _external(tmp, name, *, present, **kw):
    """An external rooted at a real directory, with or without its tool in bin/."""
    import os

    prefix = os.path.join(tmp, name)
    os.makedirs(os.path.join(prefix, "bin"), exist_ok=True)
    if present:
        open(os.path.join(prefix, "bin", name), "w").close()
    return FakeDep(name, installed=False, external=True, external_path=prefix, **kw)


def test_externals_are_not_required_to_be_installed():
    """An external has no store prefix to be installed into, so the 'spack gc'
    check must keep ignoring it -- as long as its tool is really there."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        spec = FakeSpec(build_deps=[_external(tmp, "gmake", present=True)])
        msg = _with_patterns((r"^gmake$",), lambda: _raises(lambda: build.check_buildable(spec)))
        assert msg is None


def test_external_tool_absent_on_this_host_is_reported():
    """packages.yaml claiming 'ninja prefix: /usr' is a per-host claim. Where it is
    false, Spack's own error is 'spack requires ninja', which names no record."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        spec = FakeSpec(build_deps=[_external(tmp, "ninja", present=False)])
        msg = _with_patterns((r"^ninja$",), lambda: _raises(lambda: build.check_buildable(spec)))
        assert msg and "ninja" in msg
        assert "not on this host" in msg
        assert "spack gc" not in msg, "this is not a gc casualty; the advice differs"


def test_tool_elsewhere_on_path_does_not_satisfy_the_external():
    """PATH is not a rescue, and the check must not pretend otherwise.

    Spack's ninja recipe resolves with ``which_string(name,
    path=[self.spec.prefix.bin], required=True)`` (ninja/package.py:117) -- only
    its own prefix. Accepting a ninja found elsewhere would wave through a build
    that then dies in setup_dependent_package anyway."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        elsewhere = os.path.join(tmp, "elsewhere")
        os.makedirs(elsewhere)
        open(os.path.join(elsewhere, "ninja"), "w").close()
        spec = FakeSpec(build_deps=[_external(tmp, "ninja", present=False)])
        msg = _with_patterns(
            (r"^ninja$",), lambda: _raises(lambda: build.check_buildable(spec)), path=elsewhere
        )
        assert msg and "not on this host" in msg


def test_absent_external_reports_the_prefix_that_lied():
    """The prefix is the actionable part -- it points at the packages.yaml entry."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dep = _external(tmp, "ninja", present=False)
        spec = FakeSpec(build_deps=[dep])
        msg = _with_patterns((r"^ninja$",), lambda: _raises(lambda: build.check_buildable(spec)))
        assert msg and dep.external_path in msg


def test_external_with_no_bin_directory_is_reported():
    """A prefix that has gone entirely, not merely one missing its tool."""
    spec = FakeSpec(
        build_deps=[FakeDep("ninja", installed=False, external=True, external_path="/nonexistent")]
    )
    msg = _with_patterns((r"^ninja$",), lambda: _raises(lambda: build.check_buildable(spec)))
    assert msg and "not on this host" in msg


def test_link_externals_are_not_probed():
    """Externals reached by a link edge are libraries; an absent executable says
    nothing about them. Only pure build edges are tools."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dep = _external(tmp, "openssl", present=False, depflag=dt.BUILD | dt.LINK)
        spec = FakeSpec(build_deps=[dep])
        msg = _with_patterns((r"^openssl$",), lambda: _raises(lambda: build.check_buildable(spec)))
        assert msg is None


def test_external_declaring_no_executables_fails_open():
    """Nothing to probe means no opinion -- never block a build on a guess."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        spec = FakeSpec(build_deps=[_external(tmp, "cetmodules", present=False)])
        assert _with_patterns((), lambda: _raises(lambda: build.check_buildable(spec))) is None


def test_external_provided_by_a_module_is_not_probed():
    """A module system puts it on PATH; there is no prefix to inspect."""
    dep = FakeDep(
        "ninja", installed=False, external=True, external_modules=["ninja/1.10.2"]
    )
    spec = FakeSpec(build_deps=[dep])
    assert _with_patterns((r"^ninja$",), lambda: _raises(lambda: build.check_buildable(spec))) is None


def test_uninstalled_deps_are_reported_before_absent_externals():
    """Both messages are true at once; the gc one is the more fundamental."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        spec = FakeSpec(
            build_deps=[FakeDep("gcc", installed=False), _external(tmp, "ninja", present=False)]
        )
        msg = _with_patterns((r"^ninja$",), lambda: _raises(lambda: build.check_buildable(spec)))
        assert msg and "spack gc" in msg and "gcc" in msg


def test_unbuilt_dev_dependency_gets_its_own_message():
    """A dev dep lives in the dev area and is never registered, so 'not installed'
    would be misleading -- it just has not been built yet."""
    spec = FakeSpec(build_deps=[FakeDep("cetlib-except", installed=False, dev=True)])
    msg = _raises(lambda: build.check_buildable(spec))
    assert msg and "not been built" in msg
    assert "spack gc" not in msg


def test_dev_dependency_already_built_is_accepted():
    """Built into the dev area: installed is False but the prefix is right there."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dep = FakeDep("cetlib-except", installed=False, dev=True)
        dep.prefix = tmp
        assert _raises(lambda: build.check_buildable(FakeSpec(build_deps=[dep]))) is None
        assert os.path.isdir(tmp)


def test_every_missing_dep_is_listed_at_once():
    spec = FakeSpec(
        build_deps=[FakeDep(n, installed=False) for n in ("catch2", "gcc", "ninja")]
    )
    msg = _raises(lambda: build.check_buildable(spec))
    assert msg and all(n in msg for n in ("catch2", "gcc", "ninja"))


def test_runpath_override_targets_shared_linking():
    """Must be the dict form: Configuration.set cannot create intermediate keys, so
    the dotted 'config:shared_linking:type' form raises KeyError."""
    key, value = build.LINKING_OVERRIDE
    assert key == "config:shared_linking"
    assert value == {"type": "runpath"}


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
