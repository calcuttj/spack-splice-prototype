"""Tests for base-spec resolution helpers."""

try:
    from spack.extensions.splice import base
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import os

    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import base

import spack.spec


class FakeArch:
    def __init__(self, os_):
        self.os = os_


class FakeSpec:
    def __init__(self, os_):
        self.architecture = FakeArch(os_)


def test_host_mismatch_detected():
    """A base spec from another container must be flagged.

    Spack's install layout has no OS component, so two containers' packages share
    one directory tree and nothing in the path reveals the mismatch.
    """
    assert base.host_os_mismatch(FakeSpec("someotheros")) == (
        "someotheros",
        str(spack.spec.ArchSpec.default_arch().os),
    )


def test_no_mismatch_on_native_spec():
    native = str(spack.spec.ArchSpec.default_arch().os)
    assert base.host_os_mismatch(FakeSpec(native)) is None


def test_missing_arch_is_not_a_mismatch():
    """Don't cry wolf when a spec has no recorded OS."""
    assert base.host_os_mismatch(FakeSpec("")) is None


# -- resolving what the user typed ----------------------------------------


class _Resolver:
    """Records which branch of base.resolve() a query takes."""

    def __init__(self, env_dirs=(), env_names=(), files=(), store_hits=()):
        import spack.environment as ev
        import spack.store

        self.calls = []
        self._saved = (
            ev.is_env_dir,
            ev.exists,
            ev.active_environment,
            os.path.isfile,
            spack.store.STORE.db.query,
            base.from_environment,
            base.from_file,
            base.from_store,
        )
        ev.is_env_dir = lambda p: p in env_dirs
        ev.exists = lambda n: n in env_names
        ev.active_environment = lambda: None
        os.path.isfile = lambda p: p in files
        spack.store.STORE.db.query = lambda q, **kw: ["hit"] if q in store_hits else []
        base.from_environment = lambda q, root=None: (
            self.calls.append(("env", q, root)),
            ("spec", "env"),
        )[1]
        base.from_file = lambda q: (self.calls.append(("file", q, None)), ("spec", "file"))[1]
        base.from_store = lambda q: (self.calls.append(("store", q, None)), ("spec", "store"))[1]

    def restore(self):
        import spack.environment as ev
        import spack.store

        (
            ev.is_env_dir,
            ev.exists,
            ev.active_environment,
            os.path.isfile,
            spack.store.STORE.db.query,
            base.from_environment,
            base.from_file,
            base.from_store,
        ) = self._saved


def _route(query, root=None, **kwargs):
    """Which branch resolve() took, plus the root it forwarded."""
    r = _Resolver(**kwargs)
    try:
        base.resolve(query, root=root)
        return r.calls[0]
    finally:
        r.restore()


def _branch(query, **kwargs):
    return _route(query, **kwargs)[0]


def test_environment_directory_is_recognised():
    assert _branch("/some/env", env_dirs={"/some/env"}) == "env"


def test_spec_file_is_recognised():
    assert _branch("/tmp/spec.json", files={"/tmp/spec.json"}) == "file"


def test_managed_environment_name_is_recognised():
    """A bare name like 'myenv' should not fall through to the store."""
    assert _branch("myenv", env_names={"myenv"}) == "env"


def test_plain_spec_goes_to_the_store():
    assert _branch("art-suite", store_hits={"art-suite"}) == "store"


def test_environment_wins_a_name_clash_with_a_package():
    """Both exist: prefer the environment, having warned."""
    assert _branch("art-suite", env_names={"art-suite"}, store_hits={"art-suite"}) == "env"


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


def test_no_argument_without_an_active_environment_is_an_error():
    r = _Resolver()
    try:
        base.resolve(None)
    except base.BaseSpecError as e:
        assert "no active environment" in str(e)
    else:
        raise AssertionError("expected BaseSpecError")
    finally:
        r.restore()


def test_root_is_forwarded_to_the_environment():
    branch, _query, root = _route("myenv", root="dunesw", env_names={"myenv"})
    assert (branch, root) == ("env", "dunesw")


def test_root_on_a_store_spec_is_rejected():
    """--root only means anything for an environment; silently ignoring it would
    leave the user thinking they had selected something."""
    r = _Resolver(store_hits={"art-suite"})
    try:
        base.resolve("art-suite", root="foo")
    except base.BaseSpecError as e:
        assert "only applies to an environment" in str(e)
    else:
        raise AssertionError("expected BaseSpecError")
    finally:
        r.restore()


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
