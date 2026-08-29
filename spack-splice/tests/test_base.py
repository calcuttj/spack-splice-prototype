"""Tests for base-spec resolution helpers."""

import os

try:
    from spack.extensions.splice import base
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import base

import spack.spec


class FakeArch:
    def __init__(self, platform):
        self.platform = platform


class FakeSpec:
    def __init__(self, platform):
        self.architecture = FakeArch(platform)


def _host_platform():
    return str(spack.spec.ArchSpec.default_arch().platform)


def test_host_mismatch_detected():
    """A base spec from another platform must be flagged.

    Spack's install layout has no OS component, so two containers' packages share
    one directory tree and nothing in the path reveals the mismatch.
    """
    assert base.host_platform_mismatch(FakeSpec("someotherplatform")) == (
        "someotherplatform",
        _host_platform(),
    )


def test_no_mismatch_on_native_spec():
    assert base.host_platform_mismatch(FakeSpec(_host_platform())) is None


def test_missing_arch_is_not_a_mismatch():
    """Don't cry wolf when a spec has no recorded platform."""
    assert base.host_platform_mismatch(FakeSpec("")) is None


# -- resolving what the user typed ----------------------------------------


class FakeRoot:
    def __init__(self, name, dag, version="1.0"):
        self.name = name
        self._dag = dag
        self.version = version

    def dag_hash(self):
        return self._dag

    def satisfies(self, other):
        # Name matching is all these tests need; real version/variant matching is
        # Spec.satisfies' job and is exercised by the acceptance run.
        return not other.name or other.name == self.name

    def format(self, _fmt):
        return f"{self.name}@{self.version}/{self._dag[:7]}"


def test_root_selected_by_name():
    roots = [FakeRoot("art-suite", "aaa1111"), FakeRoot("cetlib-except", "bbb2222")]
    (got,) = base.matching_roots(roots, "cetlib-except")
    assert got.name == "cetlib-except"


def test_root_selected_by_hash_prefix():
    roots = [FakeRoot("art-suite", "aaa1111"), FakeRoot("cetlib-except", "bbb2222")]
    (got,) = base.matching_roots(roots, "/bbb22")
    assert got.name == "cetlib-except"


def test_hash_prefix_that_matches_nothing_returns_empty():
    assert base.matching_roots([FakeRoot("art-suite", "aaa1111")], "/zzz") == []


def test_unparseable_root_query_matches_nothing():
    assert base.matching_roots([FakeRoot("art-suite", "aaa1111")], "@@@bad@@@") == []


# -- where resolve() looks, and in what order -----------------------------


class FakeEnv:
    def __init__(self, path):
        self.path = path


class _Resolver:
    """Stubs out the two leaf lookups so resolve()'s routing is what gets tested."""

    def __init__(self, active=None, active_roots=()):
        self._saved = (
            base.active_environment,
            base.concrete_roots,
            base.from_environment,
            base.from_store,
            base.require_installed,
        )
        base.active_environment = lambda: FakeEnv(active) if active else None
        base.concrete_roots = lambda name: (name, list(active_roots))
        base.from_environment = lambda name, spec=None: ("spec", f"env:{name}")
        base.from_store = lambda query: ("spec", f"store:{query}")
        base.require_installed = lambda spec, source: spec

    def restore(self):
        (
            base.active_environment,
            base.concrete_roots,
            base.from_environment,
            base.from_store,
            base.require_installed,
        ) = self._saved


def _source(spec=None, env=None, **kwargs):
    """Where resolve() ended up getting the spec from."""
    r = _Resolver(**kwargs)
    try:
        return base.resolve(spec, env=env)[1]
    finally:
        r.restore()


def _error(spec=None, env=None, **kwargs):
    r = _Resolver(**kwargs)
    try:
        base.resolve(spec, env=env)
    except base.BaseSpecError as e:
        return str(e)
    else:
        raise AssertionError("expected BaseSpecError")
    finally:
        r.restore()


def test_explicit_env_is_used():
    assert _source("dunesw", env="myenv") == "env:myenv"


def test_explicit_env_beats_the_active_one():
    """--env is a deliberate choice and must not be second-guessed."""
    assert _source("art-suite", env="myenv", active="/active") == "env:myenv"


def test_active_environment_is_preferred_over_the_store():
    roots = [FakeRoot("art-suite", "aaa1111")]
    assert _source("art-suite", active="/active", active_roots=roots) == "env:/active"


def test_spec_absent_from_the_active_environment_falls_back_to_the_store():
    """A site-wide install is a legitimate base; missing from the active
    environment is a miss, not an error."""
    roots = [FakeRoot("art-suite", "aaa1111")]
    assert _source("dunesw", active="/active", active_roots=roots) == "store:dunesw"


def test_spec_without_any_environment_goes_to_the_store():
    assert _source("art-suite") == "store:art-suite"


def test_ambiguity_within_the_active_environment_is_an_error():
    """Two matches is a coin toss, so make the user narrow it."""
    roots = [FakeRoot("art-suite", "aaa1111"), FakeRoot("art-suite", "bbb2222")]
    assert "matches 2 roots" in _error("art-suite", active="/active", active_roots=roots)


def test_no_spec_uses_the_active_environment():
    assert _source(None, active="/active") == "env:/active"


def test_no_spec_and_no_active_environment_is_an_error():
    assert "no active environment" in _error(None)


# -- what gets recorded in the dev area ------------------------------------


def test_env_source_yields_its_directory():
    assert base.env_of("env:/some/where/my-env") == "/some/where/my-env"


def test_store_source_yields_no_environment():
    assert base.env_of("store:boost") is None


def test_environment_directory_is_recorded_absolute():
    """The dev area is read from a different cwd than the one init ran in, so a
    relative --env must not survive into splice.yaml."""
    import spack.environment as ev

    saved = ev.as_env_dir
    ev.as_env_dir = lambda n: n  # as_env_dir passes a relative directory through
    try:
        assert os.path.isabs(base.env_dir("my-env"))
    finally:
        ev.as_env_dir = saved


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
