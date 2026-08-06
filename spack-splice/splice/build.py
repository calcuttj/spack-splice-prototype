"""Fetching sources and compiling dev packages.

Builds are driven through Spack's own builder rather than ``PackageInstaller``.
The installer computes prefixes from the store layout and registers what it builds
in the store database -- both of which we specifically do not want, since the whole
point is that a dev area is disposable and never touches the shared install tree.

Going one level down (``setup_package`` + the builder's phases) keeps full recipe
fidelity: ``cmake_args``, patches, ``setup_build_environment``, cetmodules quirks and
everything else run exactly as they would under ``spack install``.
"""

import os
import re

import spack.build_environment
import spack.builder
import spack.config
import spack.error
import spack.fetch_strategy
import spack.llnl.util.filesystem as fs
import spack.llnl.util.tty as tty
import spack.repo
import spack.stage
from spack.context import Context

#: Spack emits DT_RPATH by default, which the loader searches *before*
#: LD_LIBRARY_PATH and which therefore cannot be shadowed. Dev builds get
#: DT_RUNPATH instead. The switch is read from config at
#: ``build_environment.py:795``, so overriding it per build is enough -- no global
#: config is touched, and the installed tree keeps its rpaths.
#:
#: It has to be written as a dict rather than the obvious
#: ``("config:shared_linking:type", "runpath")``: ``Configuration.set`` does not
#: create intermediate keys (``config.py:935``), so the three-level dotted form
#: raises ``KeyError: 'shared_linking'``. Sibling keys such as ``bind`` still come
#: through from the lower-priority scopes.
LINKING_OVERRIDE = ("config:shared_linking", {"type": "runpath"})


class BuildError(spack.error.SpackError):
    """Raised when a dev package cannot be built."""


def fetch_source(node, dest: str) -> None:
    """Download ``node``'s source into ``dest``.

    Follows ``spack develop``'s approach (``cmd/develop.py:78``): build the package
    class directly rather than going through ``Spec.package``, stage it, then steal
    the staged tree.

    ``node`` must be the *installed* spec, not the dev one -- a spec carrying
    ``dev_path`` gets a ``DevelopStage``, which has no fetcher to steal from.
    """
    if os.path.exists(dest) and os.listdir(dest):
        tty.msg(f"{node.name}: source already present at {dest}")
        return

    pkg_cls = spack.repo.PATH.get_pkg_class(node.name)
    package = pkg_cls(node)
    stage = package.stage[0]

    if isinstance(stage.fetcher, spack.fetch_strategy.GitFetchStrategy):
        # A cached or mirrored clone may have truncated history, which is useless
        # for development.
        stage.fetcher.get_full_repo = True
        stage.default_fetcher_only = True

    stage.fetcher.set_package(package)
    tty.msg(f"{node.name}: fetching source into {dest}")
    package.stage.steal_source(dest)


def check_buildable(spec) -> None:
    """Fail early if the build tools this spec needs are missing.

    Three distinct problems, all of which otherwise surface as baffling cmake or
    Spack errors (``CMAKE_CXX_COMPILER not set``, ``unable to find a build program
    corresponding to "Ninja"``, ``spack requires 'ninja'``):

    * The spec records no compiler at all. Nothing can be done about that without
      the concretizer.
    * The spec records its build dependencies, but they are **no longer
      installed**. This is the normal aftermath of ``spack gc``, which removes
      build-only dependencies once nothing links against them. The concrete specs
      survive in ``<prefix>/.spack/spec.json``, so they can be reinstalled exactly
      as recorded -- still no solver required.
    * A build tool is **external, and the external is not true on this host**. See
      ``_external_tool_present``.
    """
    import spack.deptypes as dt

    if not any(spec.dependencies(virtuals=lang) for lang in ("c", "cxx", "fortran")):
        raise BuildError(
            f"{spec.name}'s spec records no compiler (nothing provides c/cxx/fortran)",
            "Splice cannot invent one without the concretizer. Pick a base spec whose "
            "build dependencies were recorded.",
        )

    # A dependency is usable if it is external, registered in the store, or simply
    # present on disk. That last case matters: a dev dependency built earlier in
    # this same run lives in the dev area and is deliberately never registered, so
    # `installed` is False for it even though it is right there.
    gone = [
        e.spec
        for e in spec.edges_to_dependencies(depflag=dt.BUILD)
        if not e.spec.external and not e.spec.installed and not _prefix_exists(e.spec)
    ]
    if gone:
        listing = "\n  ".join(
            d.format("{name}{@version}{/hash:7}") for d in sorted(gone, key=lambda x: x.name)
        )
        if any(d.variants.get("dev_path") for d in gone):
            raise BuildError(
                f"{spec.name} depends on dev packages that have not been built yet:"
                f"\n  {listing}",
                "Build the whole dev set, or name them before their dependents.",
            )
        raise BuildError(
            f"{spec.name} needs {len(gone)} build dependencies that are not installed:"
            f"\n  {listing}",
            "They were most likely removed by 'spack gc', which prunes build-only "
            "dependencies. Their exact concrete specs are still recorded, so reinstalling "
            "them restores the build without any concretization.",
        )

    # Externals were skipped above because they have no store prefix to be installed
    # into. That makes them the one class of dependency nothing has verified, so probe
    # the build-only ones -- they are tools, and a tool that is not there is fatal.
    absent = [
        e.spec
        for e in spec.edges_to_dependencies(depflag=dt.BUILD)
        if e.spec.external and e.depflag == dt.BUILD and not _external_tool_present(e.spec)
    ]
    if not absent:
        return

    listing = "\n  ".join(
        d.format("{name}{@version}{/hash:7}") + f" -> {_external_prefix(d)}"
        for d in sorted(absent, key=lambda x: x.name)
    )
    raise BuildError(
        f"{spec.name} needs {len(absent)} external build tools that are not on this "
        f"host:\n  {listing}",
        "The external is declared in packages.yaml but its executable is not under "
        "that prefix here. This is normal for a store shared between hosts -- the "
        "record is true on the machine it was written for. Build there, or make the "
        "prefix true here (in a container, bind-mount a working one onto that path). "
        "PATH will not rescue it: recipes such as ninja search only their own "
        "prefix/bin. Editing packages.yaml would reconcretize the stack.",
    )


def _prefix_exists(spec) -> bool:
    try:
        return os.path.isdir(str(spec.prefix))
    except Exception:  # noqa: BLE001 -- an unresolvable prefix is simply not there
        return False


def _external_prefix(spec) -> str:
    return getattr(spec, "external_path", None) or str(spec.prefix)


def _executable_patterns(spec):
    """Regexes for the executables a package provides, taken from its recipe.

    Spack's own external *detection* already records these -- ``ninja`` declares
    ``^ninja$`` -- so probing for them keeps no second list in sync. An empty
    result means "nothing to probe", and every caller fails open on it.
    """
    try:
        pkg_cls = spack.repo.PATH.get_pkg_class(spec.name)
    except Exception:  # noqa: BLE001 -- an unknown package is simply unprobeable
        return ()
    return tuple(getattr(pkg_cls, "executables", None) or ())


def _external_tool_present(spec) -> bool:
    """Whether an external build tool is really on this host.

    An external is a *claim* made by ``packages.yaml``, not an observation, and the
    claim is per-host. One store shared between a debian12 machine and an el9
    container carries a single ``ninja @1.10.2 prefix: /usr`` record which is true
    on only one of them; ``spec.installed`` is True for an external regardless.
    Spack trusts it, puts ``/usr/bin`` on PATH, then dies with ``spack requires
    'ninja'. Make sure it is in your path.`` from ``which(..., required=True)``.

    Only the prefix is probed, deliberately -- PATH is not a rescue. Spack's own
    ``ninja`` recipe pins the lookup to the node's own prefix::

        # spack_repo/builtin/packages/ninja/package.py:117
        which_string(name, path=[self.spec.prefix.bin], required=True)

    so a working ninja earlier on PATH does not help, and a check that accepted
    one would pass a build that then fails anyway. What has to become true is the
    record itself: something has to be at ``<prefix>/bin``.
    """
    if getattr(spec, "external_modules", None):
        return True  # a module system provides it; there is nothing on disk to probe

    patterns = _executable_patterns(spec)
    if not patterns:
        return True

    try:
        entries = os.listdir(os.path.join(_external_prefix(spec), "bin"))
    except OSError:  # no bin/ at all -- the prefix is bare or gone
        return False

    regexes = [re.compile(p) for p in patterns]
    return any(r.search(e) for r in regexes for e in entries)


def build_one(spec, prefix: str, jobs=None, stop_at=None) -> None:
    """Compile ``spec`` from its ``dev_path`` sources and install it into ``prefix``.

    ``Spec.set_prefix`` overrides the store layout, so nothing lands in the store
    and nothing is registered in its database.
    """
    check_buildable(spec)

    source = spec.variants.get("dev_path")
    if not source:
        raise BuildError(f"{spec.name} has no dev_path; it is not a dev package")
    if not os.path.isdir(source.value):
        raise BuildError(
            f"{spec.name}: no source at {source.value}",
            "Run 'spack splice src' to fetch it, or point at a checkout with "
            "'spack splice add --path'.",
        )

    spec.set_prefix(prefix)
    pkg = spec.package

    if jobs:
        spack.config.set("config:build_jobs", jobs, scope="command_line")

    with spack.config.override(*LINKING_OVERRIDE):
        spack.build_environment.setup_package(pkg, dirty=False, context=Context.BUILD)
        builder = spack.builder.create(pkg)
        # Phases run with the source directory as cwd, the same as
        # ``installer.py:2704`` does. CMake and Autotools builders cd into their own
        # build directory regardless, but a generic package's ``install()`` may just
        # run ``./install.sh`` and relies on this.
        with fs.working_dir(pkg.stage.source_path):
            for phase in builder:
                tty.msg(f"{spec.name}: {phase.name}")
                phase.execute()
                if stop_at and phase.name == stop_at:
                    tty.msg(f"{spec.name}: stopping after '{stop_at}' as requested")
                    return


def linking_type_of(path: str):
    """Report whether an ELF file carries RUNPATH, RPATH, both or neither.

    Used by ``splice status`` and worth checking after a build: if a dev binary
    comes out with RPATH, LD_LIBRARY_PATH cannot shadow anything and the whole
    scheme quietly stops working.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["readelf", "-d", path], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    tags = {t for t in ("RUNPATH", "RPATH") if f"({t})" in out}
    return tags or set()
