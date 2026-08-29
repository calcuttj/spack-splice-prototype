"""The runtime environment for a dev area.

Built from the *installed* base spec rather than the spliced one. That is
deliberate: every node in the base spec has a real prefix on disk, whereas the
spliced DAG contains rewired nodes whose new hashes correspond to nothing that was
ever installed, so asking them for a prefix yields a path that does not exist.

The dev builds are then layered on top by prepending their prefixes. Because they
are linked with DT_RUNPATH rather than DT_RPATH, ``LD_LIBRARY_PATH`` wins and the
dev libraries shadow the installed ones -- for everything except an already
installed binary that links a dev package directly, which keeps its DT_RPATH. See
``graph.unshadowable``.
"""

import os

import spack.util.tty as tty
import spack.user_environment
import spack.util.environment as senv

#: The one thing the recipes cannot give us. Spack resolves libraries through
#: RPATH, so it deliberately never puts anything on ``LD_LIBRARY_PATH`` -- neither
#: prefix inspections (``user_environment.py:19``, which cover only PATH, MANPATH,
#: ACLOCAL_PATH, PKG_CONFIG_PATH and CMAKE_PREFIX_PATH) nor the FNAL recipes, which
#: set CET_PLUGIN_PATH and friends but not this.
#:
#: Splice's whole shadowing mechanism runs on LD_LIBRARY_PATH beating DT_RUNPATH, so
#: it has to add this floor itself. Verified the hard way: deriving *everything* from
#: the recipes left a built ``cetlib-except`` absent from LD_LIBRARY_PATH entirely,
#: which silently breaks shadowing while looking fine.
REQUIRED_PATH_VARS = {"LD_LIBRARY_PATH": ("lib", "lib64")}


def modifications(state, root, dev_specs):
    """Environment modifications that put the dev builds in front of the stack.

    ``root`` is the installed base spec; ``dev_specs`` are the built dev specs, each
    already carrying ``set_prefix`` into the dev area.

    Both halves come from Spack's own machinery rather than a table of directory
    names of ours. That matters: every FNAL recipe sets these variables from *its
    own prefix* -- ``art/package.py:103`` does
    ``env.prepend_path("CET_PLUGIN_PATH", prefix.lib)``, ``artg4tk`` appends
    ``f"{self.prefix}/fcl"``, and ``python/package.py:1311`` uses
    ``dependent_spec.prefix`` for ``PYTHONPATH``. So a dev spec pointed at the dev
    area emits dev-area entries by itself.

    A hand-written table cannot keep up with this. Measured against ``dunecore``, the
    real conventions are ``fcl`` *and* ``job`` (LArSoft) for ``FHICL_FILE_PATH``;
    ``gdml``, ``config_data``, ``compatibility``, ``scripts`` and ``G4`` for
    ``FW_SEARCH_PATH``; ``perllib`` for ``PERL5LIB``; ``include`` plus the prefix for
    ``ROOT_INCLUDE_PATH``; and ``lib/python`` or ``lib/pythonX.Y/site-packages`` for
    ``PYTHONPATH``. Letting the recipes decide gets all of that right for free.

    Order matters: the base stack is applied first and the dev specs second, so the
    dev prepends end up in front.
    """
    env = spack.user_environment.environment_modifications_for_specs(root)

    usable = [s for s in dev_specs if os.path.isdir(str(s.prefix))]
    for spec in dev_specs:
        if spec not in usable:
            tty.debug(f"{spec.name}: no build at {spec.prefix}, skipping in env")
    if usable:
        env.extend(spack.user_environment.environment_modifications_for_specs(*usable))

    # ...plus the floor Spack never provides. Last, so it lands in front.
    for spec in usable:
        for var, subdirs in REQUIRED_PATH_VARS.items():
            for sub in subdirs:
                path = os.path.join(str(spec.prefix), sub)
                if os.path.isdir(path):
                    env.prepend_path(var, path)

    # The dev specs drag their installed dependencies along, so the same store paths
    # get contributed twice. Harmless but noisy, and it makes the packed script
    # needlessly long.
    for var in {m.name for m in env.env_modifications}:
        env.prune_duplicate_paths(var)

    env.set("SPACK_SPLICE_DIR", state.path)
    return env


def path_variables(env):
    """Names of the variables that are search paths rather than plain values.

    Needed when freezing an environment into a script: a search path has to be
    *prepended* to whatever the target already has, while a plain value is just set.
    Get this wrong for ``PATH`` and the target shell loses ``/usr/bin``.
    """
    return {
        m.name for m in env.env_modifications if isinstance(m, senv.NamePathModifier)
    }


def built(prefixes):
    """The subset of dev packages that actually have something installed."""
    return {n: p for n, p in prefixes.items() if os.path.isdir(p)}


def shell_code(env, shell: str = "sh") -> str:
    return env.shell_modifications(shell=shell, explicit=True)


def as_dict(env):
    """Apply the modifications to a copy of the environment and return the result.

    Used for ``--json`` and for inspecting what a variable will end up as without
    touching the caller's environment.
    """
    scratch = dict(os.environ)
    env.apply_modifications(scratch)
    return scratch


def summary(env, prefixes, keys=("PATH", "LD_LIBRARY_PATH", "CET_PLUGIN_PATH")):
    """Human-readable report: which dev prefix leads each search path."""
    resolved = as_dict(env)
    lines = []
    for key in keys:
        value = resolved.get(key, "")
        first = value.split(os.pathsep)[0] if value else "(unset)"
        owner = next((n for n, p in prefixes.items() if first.startswith(p)), None)
        lines.append((key, first, owner))
    return lines


#: Re-exported so callers do not need spack.util.environment directly.
EnvironmentModifications = senv.EnvironmentModifications
