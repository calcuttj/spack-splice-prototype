"""Loading the base concrete spec. Solver-free, by construction.

Three sources are accepted, none of which concretizes:

* the store, by name or ``/hash``
* a Spack environment directory (``Environment`` reads ``spack.lock`` on construction)
* a raw ``spec.json`` / ``spack.lock`` file

The spec named on the command line is always a *root*: either a root of the
environment being used, or an installed spec in the store.
"""

import os

import spack.deptypes as dt
import spack.environment as ev
import spack.environment.environment as ee
import spack.error
import spack.util.tty as tty
import spack.spec
import spack.store

#: Deptypes that put a dependency on the runtime link line. Only these decide
#: whether an installed spec is usable as a base.
RUNTIME = dt.LINK | dt.RUN


class BaseSpecError(spack.error.SpackError):
    """Raised when the base spec can't be resolved to exactly one concrete spec."""


def enrich_from_prefix(spec):
    """Re-read a spec from its install prefix, which is the authoritative copy.

    The database can hold a *lossy* record. Some installs are registered without
    their build dependencies, so the DB spec has no compiler node at all and
    ``spack find`` files it under "no compilers" -- while
    ``<prefix>/.spack/spec.json`` has the full graph, gcc included. Observed here
    on packages that were unquestionably built from source (their prefixes still
    carry ``spack-build-out.txt.gz``).

    That matters a lot for splice: without build dependencies there is no compiler
    to inherit and nothing can be rebuilt. Both copies carry the same dag hash, so
    preferring the richer one is safe.
    """
    try:
        specfile = os.path.join(str(spec.prefix), ".spack", "spec.json")
    except spack.error.SpecError:
        return spec
    if not os.path.isfile(specfile):
        return spec

    try:
        full = spack.spec.Spec.from_specfile(specfile)
    except Exception as e:  # noqa: BLE001 -- a corrupt specfile must not be fatal
        tty.debug(f"could not read {specfile}: {e}")
        return spec

    if full.dag_hash() != spec.dag_hash():
        tty.debug(f"{specfile} hash differs from the database record; using the database")
        return spec
    return full


def active_environment():
    """The active environment, or None.

    Wrapped because ``spack.environment`` does not re-export it -- ``ev.active``
    is a different function that asks whether a *named* environment is active.
    """
    return ee.active_environment()


def known_environments_hint() -> str:
    known = sorted(ev.all_environment_names())
    return f"Known environments: {', '.join(known)}." if known else "No environments exist."


def env_dir(name_or_dir: str) -> str:
    """Resolve an environment *name* or directory to an absolute directory.

    ``ev.as_env_dir`` (``environment.py:320``) accepts either, but its own error
    for a name that does not exist says nothing about what does, and it hands back
    a relative directory unchanged. Absolute is what we want: the result gets
    recorded in the dev area, which is read from a different cwd.
    """
    try:
        return os.path.abspath(ev.as_env_dir(name_or_dir))
    except Exception as e:  # noqa: BLE001 -- any failure here means "not an environment"
        raise BaseSpecError(
            f"no such environment: '{name_or_dir}'",
            f"{known_environments_hint()} A directory containing spack.lock also works.",
        ) from e


def from_store(query: str):
    """Look up an installed spec by name, spec string, or ``/hash``."""
    matches = spack.store.STORE.db.query(query)
    if not matches:
        raise BaseSpecError(
            f"no installed spec matches '{query}'",
            "Use --env to look in an environment instead. "
            f"{known_environments_hint()}",
        )
    if len(matches) > 1:
        listing = "\n  ".join(m.format("{name}{@version}{/hash:7}") for m in matches)
        raise BaseSpecError(
            f"'{query}' matches {len(matches)} installed specs, be more specific:\n  {listing}"
        )
    return enrich_from_prefix(matches[0]), f"store:{query}"


def _installed(spec) -> bool:
    try:
        return bool(spec.installed)
    except Exception:  # noqa: BLE001 -- an unresolvable spec is simply not installed
        return False


def matching_roots(roots, query: str):
    """Roots of an environment that match ``query``.

    ``/abc123`` selects by dag hash prefix; anything else is parsed as a spec and
    matched with ``satisfies``, so ``dunesw``, ``dunesw@10.11.01d00`` and
    ``dunesw+cuda`` all work.
    """
    if query.startswith("/"):
        prefix = query[1:]
        return [r for r in roots if r.dag_hash().startswith(prefix)]
    try:
        wanted = spack.spec.Spec(query)
    except Exception:  # noqa: BLE001 -- an unparseable query simply matches nothing
        return []
    return [r for r in roots if r.satisfies(wanted)]


def concrete_roots(name_or_dir: str):
    """``(directory, roots)`` for an environment, without concretizing.

    The directory is the resolved one, so a later rehydrate does not depend on the
    name still mapping to the same place.
    """
    path = env_dir(name_or_dir)
    roots = list(ev.Environment(path).concrete_roots())
    if not roots:
        raise BaseSpecError(
            f"environment '{name_or_dir}' has no concretized roots",
            "Run 'spack concretize' in it first.",
        )
    return path, roots


def _listing(specs) -> str:
    return "\n  ".join(s.format("{name}{@version}{/hash:7}") for s in specs)


def from_environment(name_or_dir: str, spec: str = None):
    """Select one root of an environment, by spec or ``/hash``.

    With no ``spec`` the environment must have exactly one root; otherwise the user
    has to say which one, since there is nothing else to go on.
    """
    path, roots = concrete_roots(name_or_dir)

    if spec:
        matches = matching_roots(roots, spec)
        if not matches:
            raise BaseSpecError(
                f"'{spec}' is not a root of '{name_or_dir}'",
                f"Its roots are:\n  {_listing(roots)}",
            )
        if len(matches) > 1:
            raise BaseSpecError(
                f"'{spec}' matches {len(matches)} roots of '{name_or_dir}':"
                f"\n  {_listing(matches)}",
                "Narrow it, or select one by /hash.",
            )
        return matches[0], f"env:{path}"

    if len(roots) > 1:
        raise BaseSpecError(
            f"environment '{name_or_dir}' has {len(roots)} roots:\n  {_listing(roots)}",
            f"Name the one you want, e.g. '{roots[0].name}' "
            f"or '/{roots[0].dag_hash()[:7]}'",
        )
    return roots[0], f"env:{path}"


def host_platform_mismatch(spec):
    """Return ``(spec_platform, host_platform)`` across a linux/darwin/windows split.

    Only the platform, not the distro: an Alma base is fine to develop against on
    Debian, and warning about every such pair would be noise. Note this does still
    let one case through -- glibc compatibility is forward-only, so a base built
    against a *newer* glibc than this host's will not run here, whatever the
    platform says.

    Worth checking at all because Spack's install layout has no OS component --
    everything lands in ``opt/spack/linux-<target>/`` -- so one store can hold
    specs from several containers with nothing in the path to tell them apart.
    """
    host = str(spack.spec.ArchSpec.default_arch().platform)
    spec_platform = str(spec.architecture.platform)
    if spec_platform and spec_platform != host:
        return spec_platform, host
    return None


def warn_on_host_mismatch(spec) -> None:
    mismatch = host_platform_mismatch(spec)
    if mismatch:
        spec_platform, host_platform = mismatch
        tty.warn(
            f"base spec was built for {spec_platform}, but this host is "
            f"{host_platform}. Dev builds made here will not be ABI-compatible "
            "with it, and the installed binaries will not run either."
        )


def require_installed(spec, source: str):
    """Fail unless ``spec`` is installed, and return the richer on-disk copy.

    An environment's concrete roots are not necessarily installed, so this cannot
    be left implicit: only the store lookup guarantees it.
    """
    if not _installed(spec):
        raise BaseSpecError(
            f"{spec.format('{name}{@version}{/hash:7}')} (from {source}) is concrete "
            "but not installed",
            "Splice develops against an installed stack. Install it first.",
        )
    spec = enrich_from_prefix(spec)

    missing = [d for d in spec.traverse(root=False, deptype=RUNTIME) if not _installed(d)]
    if missing:
        tty.warn(
            f"{len(missing)} runtime dependencies of the base spec are not installed; "
            "they cannot go in the dev_view and builds against them will fail:",
            *sorted({d.name for d in missing}),
        )
    return spec


def _from_active_or_store(spec: str):
    """A root of the active environment matching ``spec``, else the store.

    The active environment is only a *preference*: missing there is not an error,
    because a site-wide install is an equally legitimate base. An ambiguous match
    within it is an error, since silently picking one would be a coin toss.
    """
    active = active_environment()
    if active is not None:
        _path, roots = concrete_roots(active.path)
        matches = matching_roots(roots, spec)
        if len(matches) > 1:
            raise BaseSpecError(
                f"'{spec}' matches {len(matches)} roots of the active environment:"
                f"\n  {_listing(matches)}",
                "Narrow it, or select one by /hash.",
            )
        if matches:
            return matches[0], f"env:{active.path}"
    return from_store(spec)


def resolve(spec: str = None, env: str = None):
    """Resolve to ``(concrete_spec, source_description)``. Never concretizes.

    ``spec`` names a root: a root of ``env`` when given, otherwise a root of the
    active environment, otherwise an installed spec in the store. Omitting it is
    only meaningful for a single-root environment.
    """
    if env is not None:
        found, source = from_environment(env, spec)
    elif spec is not None:
        found, source = _from_active_or_store(spec)
    else:
        active = active_environment()
        if active is None:
            raise BaseSpecError(
                "no spec given and no active environment",
                "Pass an installed spec or /hash, name an environment with --env, or "
                "activate one first.",
            )
        found, source = from_environment(active.path, None)

    return require_installed(found, source), source


def env_of(source: str):
    """The environment directory behind a ``source``, or None if it came from the store.

    The directory rather than its ``spack.yaml``: that is what ``ev.Environment``
    takes, and the manifest is always ``<dir>/spack.yaml`` anyway.
    """
    kind, _, locator = source.partition(":")
    return locator if kind == "env" else None


def reload(base_hash: str, environment: str = None):
    """Re-load the concrete spec a dev area was created against.

    Goes back to the environment ``init`` used, when there was one, rather than
    assuming the store: an environment's concrete roots are not necessarily
    installed, and its lock is the only place the exact DAG is written down.

    Selects by the recorded hash, never by name, so that a re-concretized
    environment is reported as such instead of silently swapping the base out.
    """
    if environment:
        if not os.path.isdir(environment):
            raise BaseSpecError(
                f"the environment this dev area was created from is gone: {environment}",
                "Re-run 'spack splice init'.",
            )
        _path, roots = concrete_roots(environment)
        spec = next((r for r in roots if r.dag_hash() == base_hash), None)
        if spec is None:
            raise BaseSpecError(
                f"{environment} no longer has a root with hash {base_hash[:7]}:"
                f"\n  {_listing(roots)}",
                "It has been re-concretized since 'spack splice init'. Re-run init.",
            )
        source = f"env:{environment}"
    else:
        found = spack.store.STORE.db.get_by_hash(base_hash)
        if not found:
            raise BaseSpecError(
                f"base spec {base_hash[:7]} is no longer installed",
                "The dev area is stale. Re-run 'spack splice init'.",
            )
        spec = found[0] if isinstance(found, list) else found
        source = f"store:{base_hash[:7]}"

    return require_installed(spec, source)
