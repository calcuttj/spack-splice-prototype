"""``spack splice`` -- develop part of an installed spec without concretizing."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
import yaml

import spack.cmd
import spack.error
import spack.hash_types
import spack.util.tty as tty
from spack.util.tty.colify import colify

from spack.extensions.splice import (
    base,
    build,
    check,
    graph,
    pack,
    runtime,
    specbuild,
    state,
    view,
)

description = "develop a subgraph of an installed spec without concretizing"
section = "environments" # TODO -- Consider otherwise -- new?
level = "long"

#: (name, *aliases) for each subcommand, dispatched to ``splice_<name>``.
subcommands = [
    ("init",),
    ("add",),
    ("rm", "remove"),
    ("build",),
    ("pack",),
]

_dispatch = {}


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    sp = subparser.add_subparsers(metavar="SUBCOMMAND", dest="splice_command", required=True)
    g = globals()
    for entry in subcommands:
        name, aliases = entry[0], entry[1:]
        for alias in entry:
            _dispatch[alias] = g[f"splice_{name}"]
        setup = g[f"splice_{name}_setup_parser"]
        sub = sp.add_parser(
            name,
            aliases=aliases,
            description=spack.cmd.doc_dedented(setup),
            help=spack.cmd.doc_first_line(setup),
        )
        setup(sub)


def _dir_arg(parser): ## TODO -- Need this?
    parser.add_argument(
        "-d",
        "--dir",
        default=None,
        help="splice dev area (default: search upward from cwd, or $SPACK_SPLICE_DIR)",
    )


# -- init ------------------------------------------------------------------


def splice_init_setup_parser(subparser):
    """create a dev area bound to an installed concrete spec

    The base is a root spec: a root of --env when given, otherwise a root of the
    active environment, otherwise an installed spec in the store. Omit it to use
    the sole root of the environment. Nothing is ever concretized.
    """
    subparser.add_argument(
        "spec",
        nargs="?",
        default=None,
        help="root spec to develop against, as a spec like 'dunesw' or a /hash "
        "(default: the sole root of the environment)",
    )
    subparser.add_argument(
        "--env",
        default=None,
        metavar="ENV",
        help="environment to take the root spec from, by name or directory "
        "(default: the active environment, then the site-wide specs)",
    )
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )


def splice_init(args):
    # Resolve before touching the filesystem, so a bad spec leaves no half-made
    # dev area behind. TODO -- still unchecked: that the base is installed with
    # RUNPATH, without which LD_LIBRARY_PATH cannot shadow it.
    spec, source = base.resolve(args.spec, env=args.env)

    ## Make build/ install/ fetched_srcs/ splice.yaml
    Path(args.dir).mkdir(exist_ok=True)
    dir_paths = [Path(args.dir, n) for n in ['build', 'install', 'fetched_srcs']]
    for dp in dir_paths:
        try:
            dp.mkdir(parents=True, exist_ok=False)
        except:
            tty.die(f'{args.dir} already contains {dp}')

    try:
        Path(args.dir, 'splice.yaml').touch(exist_ok=False)
    except:
        tty.die(f'{args.dir} already contains splice.yaml')
    

    # Record what it resolved *to*, not just what was typed: 'args.spec' may be
    # None or a hash prefix, and --env may be a name that later points elsewhere.
    # 'environment' is the resolved directory (holding spack.yaml and spack.lock),
    # or null when the base came from the store.
    splice_yaml_data = {
        'splice': {
            'spec': spec.format('{name}{@version}'),
            'hash': spec.dag_hash(),
            'environment': base.env_of(source),
        }
    }

    with open(Path(args.dir, 'splice.yaml'), 'w') as file:
        yaml.dump(splice_yaml_data, file, default_flow_style=False, sort_keys=False)


    tty.msg(
        f"dev area at {args.dir}",
        f"base: {spec.format('{name}{@version}{/hash:7}')} ({len(list(spec.traverse()))} nodes)",
        f"arch: {spec.architecture}",
        f"from: {source}",
    )
    base.warn_on_host_mismatch(spec)
    tty.msg("next: spack splice add <package>...")


# -- add / rm --------------------------------------------------------------


def splice_add_setup_parser(subparser):
    """choose packages within the base spec to develop locally

    Splice works out the rest of the rebuild set for you: anything lying on a
    dependency path between two chosen packages is pulled in automatically.
    """
    subparser.add_argument("packages", nargs="+", help="package names within the base spec")
    # subparser.add_argument(
    #     "-p",
    #     "--path",
    #     default=None,
    #     help="source directory (default: <dev-area>/src/<package>)",
    # )
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )
    # subparser.add_argument(
    #     "--new",
    #     action="store_true",
    #     help="add a package that is not in the base spec, built from its recipe",
    # )
    _dir_arg(subparser)


def splice_add(args):

    # TODO -- now, check that this is an init'd area: splice.yaml, fetched_srcs/, etc. need to exist.
    #         It's ok if they have contents
    #         Also check that the environment, spec, hash, etc. listed in 
    #         splice.yaml within args.dir still exist and are installed
    # root = base.rehydrate(st)

    # Then we want to check that the package is within the spec graph given in splice.yaml
    # If not, then fail but say that functionality might be added at a later date
    pass


def splice_rm_setup_parser(subparser):
    """stop developing packages"""
    subparser.add_argument("packages", nargs="+", help="package names to unpick")
    _dir_arg(subparser)


def splice_rm(args):
    st = state.find(args.dir)
    unknown = [p for p in args.packages if p not in st.picks]
    if unknown:
        tty.die(f"not currently developed: {', '.join(unknown)}")
    for name in args.packages:
        del st.picks[name]
    st.write()
    tty.msg(f"no longer developing: {', '.join(args.packages)}")


# -- build -----------------------------------------------------------------


def splice_build_setup_parser(subparser):
    """compile the dev packages into the dev area

    Builds bottom-up through Spack's own builder, so all recipe logic applies.
    Dev binaries are linked with RUNPATH rather than RPATH so that LD_LIBRARY_PATH
    can shadow the installed stack.
    """
    subparser.add_argument("packages", nargs="*", help="limit to these packages")
    subparser.add_argument("-j", "--jobs", type=int, default=None, help="build parallelism")
    subparser.add_argument(
        "-u", "--until", metavar="PHASE", default=None, help="stop after this phase"
    )
    _dir_arg(subparser)


def splice_build(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    if not st.picks:
        tty.die("nothing being developed yet. Use 'spack splice add <package>'.")

    # Recompute rather than reading the cache: dev prefixes are assigned in memory
    # by Spec.set_prefix and are not part of the serialized spec. The splice is
    # skipped -- we build the hand-built specs, which still have their build deps.
    computed = specbuild.compute(st, root, weave_dag=False)
    buildable, prefixes = computed.buildable, computed.prefixes

    order = list(computed.order)
    if args.packages:
        unknown = [p for p in args.packages if p not in order]
        if unknown:
            tty.die(f"not in the dev set: {', '.join(unknown)}")
        order = [n for n in order if n in set(args.packages)]

    for name in order:
        tty.msg(f"building {name} -> {prefixes[name]}")
        build.build_one(buildable[name], prefixes[name], jobs=args.jobs, stop_at=args.until)

    sys.stdout.flush()
    _report_linking(prefixes, order)


def _report_linking(prefixes, names):
    """Confirm the dev binaries came out with RUNPATH, not RPATH.

    Worth checking explicitly: if a build somehow emits RPATH, shadowing silently
    stops working and everything still looks fine until the wrong library loads.
    """
    for name in names:
        libs = []
        for sub in ("lib", "lib64"):
            d = os.path.join(prefixes[name], sub)
            if os.path.isdir(d):
                libs += [os.path.join(d, f) for f in os.listdir(d) if ".so" in f]
        if not libs:
            continue
        tags = build.linking_type_of(libs[0])
        if tags is None:
            continue
        if "RPATH" in tags and "RUNPATH" not in tags:
            tty.warn(f"{name}: built with RPATH -- LD_LIBRARY_PATH will NOT shadow it")
        else:
            tty.msg(f"{name}: {'/'.join(sorted(tags)) or 'no rpath'} (shadowing works)")



# -- pack ------------------------------------------------------------------


def splice_pack_setup_parser(subparser):
    """tar the dev builds together with a generated setup script

    The tarball carries only the dev builds, so it assumes the target machine
    reaches the same Spack store at the same path. Unpack anywhere and
    'source setup.sh'; relocation works because the dev builds carry DT_RUNPATH,
    which LD_LIBRARY_PATH overrides.
    """
    subparser.add_argument(
        "-o", "--output", default=None, help="output path (default: <dev-area-name>.tar.gz)"
    )
    _dir_arg(subparser)


def splice_pack(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    if not st.picks:
        tty.die("nothing being developed yet. Use 'spack splice add <package>'.")

    computed = specbuild.compute(st, root)
    spliced, buildable, prefixes = computed.spliced, computed.buildable, computed.prefixes
    present = runtime.built(prefixes)
    if not present:
        tty.die("no dev packages have been built yet. Run 'spack splice build'.")
    if len(present) < len(prefixes):
        unbuilt = sorted(set(prefixes) - set(present))
        sys.stdout.flush()
        tty.warn(f"not built, so absent from the tarball: {', '.join(unbuilt)}")

    env = runtime.modifications(st, root, [buildable[n] for n in sorted(present)])
    # Resolve against an *empty* environment, not os.environ: we want only what Spack
    # contributes, so this machine's PATH does not get baked into a portable artifact.
    resolved = {}
    env.apply_modifications(resolved)
    # ...and remember which variables are search paths, so the generated script can
    # extend the target's values instead of replacing them.
    path_vars = runtime.path_variables(env)

    output = args.output or os.path.join(os.getcwd(), os.path.basename(st.path) + ".tar.gz")
    path, digest, packed = pack.create(
        st, present, resolved, path_vars, root, output, spliced=spliced
    )

    size = os.path.getsize(path)
    tty.msg(
        f"packed {len(packed)} dev package(s): {', '.join(packed)}",
        f"{path}  ({size / 1024 / 1024:.1f} MiB)",
        f"sha256 {digest}",
    )
    tty.msg("on the target: tar xzf <file> && cd <dir> && . ./setup.sh")

# -- entry point -----------------------------------------------------------


def splice(parser, args):
    _dispatch[args.splice_command](args)
