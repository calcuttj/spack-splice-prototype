"""Loading the base concrete spec. Solver-free, by construction.

Three sources are accepted, none of which concretizes:

* the store, by name or ``/hash``
* a Spack environment directory (``Environment`` reads ``spack.lock`` on construction)
* a raw ``spec.json`` / ``spack.lock`` file
"""

import os

import spack.environment as ev
import spack.error
import spack.llnl.util.tty as tty
import spack.spec
import spack.store


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


def from_store(query: str):
    """Look up an installed spec by name, spec string, or ``/hash``."""
    matches = spack.store.STORE.db.query(query)
    if not matches:
        known = sorted(ev.all_environment_names())
        hint = (
            f"Known environments: {', '.join(known)}." if known else "No environments exist."
        )
        raise BaseSpecError(
            f"no installed spec or environment matches '{query}'",
            f"{hint} You can also pass an environment directory or a spec.json.",
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


def from_environment(name_or_dir: str, root: str = None):
    """Read the concrete roots of an environment without concretizing.

    Accepts a managed environment's *name* as well as a directory;
    ``ev.as_env_dir`` (``environment.py:320``) resolves either. The recorded source
    is always the resolved directory, so a later rehydrate does not depend on the
    name still mapping to the same place.

    A multi-root environment needs ``root`` to say which one to develop against.
    """
    path = ev.as_env_dir(name_or_dir)
    env = ev.Environment(path)
    roots = list(env.concrete_roots())
    if not roots:
        raise BaseSpecError(f"environment '{name_or_dir}' has no concretized roots")

    listing = "\n  ".join(r.format("{name}{@version}{/hash:7}") for r in roots)

    if root:
        matches = matching_roots(roots, root)
        if not matches:
            raise BaseSpecError(
                f"no root of '{name_or_dir}' matches '{root}'",
                f"Its roots are:\n  {listing}",
            )
        if len(matches) > 1:
            detail = "\n  ".join(m.format("{name}{@version}{/hash:7}") for m in matches)
            raise BaseSpecError(
                f"'{root}' matches {len(matches)} roots of '{name_or_dir}':\n  {detail}",
                "Narrow it, or select one by /hash.",
            )
        return matches[0], f"env:{path}"

    if len(roots) > 1:
        raise BaseSpecError(
            f"environment '{name_or_dir}' has {len(roots)} roots:\n  {listing}",
            f"Choose one with --root, e.g. --root {roots[0].name} "
            f"or --root /{roots[0].dag_hash()[:7]}",
        )
    return roots[0], f"env:{path}"


def from_file(path: str):
    """Read a concrete spec straight off disk."""
    spec = spack.spec.Spec.from_specfile(path)
    if not spec.concrete:
        raise BaseSpecError(f"{path} does not contain a concrete spec")
    return spec, f"file:{path}"


def host_os_mismatch(spec):
    """Return ``(spec_os, host_os)`` if the base spec was built for another OS.

    Worth checking loudly. Spack's install layout has no OS component -- everything
    lands in ``opt/spack/linux-<target>/`` -- so a store can hold specs from several
    containers with nothing in the path to tell them apart. Building dev packages
    here against a base spec from a different OS produces binaries that link against
    the wrong libc and fail at load time, which is a confusing way to find out.
    """
    host = spack.spec.ArchSpec.default_arch()
    spec_os = str(spec.architecture.os)
    if spec_os and spec_os != str(host.os):
        return spec_os, str(host.os)
    return None


def warn_on_host_mismatch(spec) -> None:
    mismatch = host_os_mismatch(spec)
    if mismatch:
        spec_os, host_os = mismatch
        tty.warn(
            f"base spec was built for {spec_os}, but this host is {host_os}. "
            "Dev builds made here will not be ABI-compatible with it, and the "
            "installed binaries most likely will not run either."
        )


def resolve(query=None, root: str = None):
    """Resolve ``query`` to ``(concrete_spec, source_description)``.

    Accepts, in order: an environment directory, a path to a spec file, the *name*
    of a managed environment, or a spec / ``/hash`` to look up in the store. With no
    argument it uses the active environment.

    Because an environment can share a name with a package, an ambiguous name
    resolves to the environment and says so, rather than silently picking one.
    """
    if query is None:
        active = ev.active_environment()
        if active is None:
            raise BaseSpecError(
                "no spec given and no active environment",
                "Pass a spec, a /hash, an environment name or directory, or activate "
                "an environment first.",
            )
        return from_environment(active.path, root)

    if ev.is_env_dir(query):
        return from_environment(query, root)
    if os.path.isfile(query):
        return from_file(query)

    if ev.exists(query):
        if spack.store.STORE.db.query(query):
            tty.warn(
                f"'{query}' is both an environment and an installed package; using the "
                f"environment. For the package, give a version or /hash instead."
            )
        return from_environment(query, root)

    if root:
        raise BaseSpecError(
            f"--root only applies to an environment, and '{query}' is not one"
        )
    return from_store(query)


def rehydrate(state):
    """Re-load the base spec recorded in a dev area's state.

    Goes back to whichever source ``init`` used, rather than assuming the store --
    an environment's concrete roots are not necessarily installed. Warns if the
    source has since been re-concretized out from under the dev area.
    """
    kind, _, locator = state.base_source.partition(":")

    if kind == "env":
        # Select by the recorded hash rather than re-asking which root to use: a
        # multi-root environment would otherwise demand --root on every command.
        # Falling back to the plain lookup keeps the "re-concretized" warning below
        # meaningful when the hash no longer exists.
        roots = list(ev.Environment(locator).concrete_roots())
        spec = next((r for r in roots if r.dag_hash() == state.base_hash), None)
        if spec is None:
            spec, _ = from_environment(locator)
        spec = enrich_from_prefix(spec) if _installed(spec) else spec
    elif kind == "file":
        spec, _ = from_file(locator)
    else:
        found = spack.store.STORE.db.get_by_hash(state.base_hash)
        if not found:
            raise BaseSpecError(
                f"base spec {state.base_hash[:7]} (from {state.base_source}) is no longer "
                "installed",
                "The dev area is stale. Re-run 'spack splice init'.",
            )
        spec = enrich_from_prefix(found[0] if isinstance(found, list) else found)

    if spec.dag_hash() != state.base_hash:
        tty.warn(
            f"{state.base_source} now resolves to {spec.dag_hash()[:7]}, but this dev area "
            f"was created against {state.base_hash[:7]}. It has been re-concretized; "
            "your dev set may no longer line up."
        )
    return spec
