"""``spack splice`` -- develop part of an installed spec without concretizing."""

import argparse
import json
import os
import sys
import time

import spack.cmd
import spack.error
import spack.hash_types
import spack.llnl.util.tty as tty
from spack.llnl.util.tty.colify import colify

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
section = "environments"
level = "long"

#: (name, *aliases) for each subcommand, dispatched to ``splice_<name>``.
subcommands = [
    ("init",),
    ("add",),
    ("rm", "remove"),
    ("status", "st"),
    ("view",),
    ("spec",),
    ("src",),
    ("build",),
    ("env",),
    ("pack",),
    ("graph",),
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


def _dir_arg(parser):
    parser.add_argument(
        "-d",
        "--dir",
        default=None,
        help="splice dev area (default: search upward from cwd, or $SPACK_SPLICE_DIR)",
    )


# -- init ------------------------------------------------------------------


def splice_init_setup_parser(subparser):
    """create a dev area bound to an installed concrete spec

    The base may be an installed spec or /hash, the name of a managed environment,
    an environment directory, or a path to a spec.json. Omit it to use the active
    environment. Nothing is ever concretized.
    """
    subparser.add_argument(
        "spec",
        nargs="?",
        default=None,
        help="installed spec, /hash, environment name or dir, or spec file "
        "(default: the active environment)",
    )
    subparser.add_argument(
        "--root",
        default=None,
        metavar="SPEC",
        help="which root to use, when the environment has more than one "
        "(a spec like 'dunesw', or /hash)",
    )
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )


def splice_init(args):
    spec, source = base.resolve(args.spec, root=args.root)
    st = state.State(path=args.dir, base_hash=spec.dag_hash(), base_source=source)
    st.write()
    tty.msg(
        f"dev area at {st.path}",
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
    subparser.add_argument(
        "-p",
        "--path",
        default=None,
        help="source directory (default: <dev-area>/src/<package>)",
    )
    subparser.add_argument(
        "--new",
        action="store_true",
        help="add a package that is not in the base spec, built from its recipe",
    )
    _dir_arg(subparser)


def splice_add(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)

    if args.path and len(args.packages) > 1:
        tty.die("--path takes a single package")

    if args.new:
        already = [p for p in args.packages if p in graph.names_in(root)]
        if already:
            tty.die(
                f"already in the base spec, drop --new: {', '.join(sorted(already))}"
            )
        names = args.packages
    else:
        names = list(graph.resolve_picks(root, args.packages))

    for name in names:
        src = os.path.abspath(args.path) if args.path else os.path.join(st.source_root, name)
        st.picks[name] = {"path": src, "new": bool(args.new)}
    st.write()

    tty.msg(f"developing: {', '.join(sorted(names))}")
    _report(st, root)


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


# -- status ----------------------------------------------------------------


def splice_status_setup_parser(subparser):
    """show the dev set, the frontier, and what cannot be shadowed"""
    _dir_arg(subparser)


def splice_status(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    print(f"dev area : {st.path}")
    print(f"base     : {root.format('{name}{@version}{/hash:7}')}  ({st.base_source})")
    print(f"arch     : {root.architecture}")
    sys.stdout.flush()
    base.warn_on_host_mismatch(root)
    if not st.picks:
        tty.msg("nothing being developed yet. Use 'spack splice add <package>'.")
        return
    _report(st, root)


def _report(st, root):
    """Shared summary used by both ``add`` and ``status``."""
    existing = [n for n, p in st.picks.items() if not p.get("new")]
    added = sorted(n for n, p in st.picks.items() if p.get("new"))

    picks = graph.resolve_picks(root, existing)
    dev = graph.dev_set(root, picks.values())
    nodes, _, _ = graph.adjacency(root, graph.PROPAGATE)

    chosen = set(picks.values())
    print()
    print(f"dev set ({len(dev) + len(added)} packages to rebuild):")
    rows = []
    for h in sorted(dev, key=lambda x: nodes[x].name):
        marker = "*" if h in chosen else " "
        rows.append(f"{marker} {nodes[h].name}@{nodes[h].version}")
    rows += [f"+ {n}" for n in added]
    colify(rows, indent=2)
    print("  (* = chosen by you, + = new, the rest are implied)")

    # One reconciliation pass, used for both the frontier and the drift report: the
    # frontier must come from the reconciled specs, since drift can add or drop
    # dependencies.
    try:
        computed = specbuild.compute(st, root, weave_dag=False)
    except spack.error.SpackError as e:
        print()
        sys.stdout.flush()
        tty.error(f"cannot reconcile with the recipes: {e}")
        return

    front = specbuild.frontier_of(computed.buildable)
    linkable, missing = view.linkable(graph.view_closure(root, front))
    note = f", {len(missing)} not installed" if missing else ""
    print(f"\nfrontier : {len(front)} direct deps -> {len(linkable)} in the dev_view{note}")
    print(f"dev_view : {st.view_root}" if os.path.islink(st.view_root) else "dev_view : not built")

    if computed.drifts:
        print(f"\nrecipe drift ({len(computed.drifts)} package(s) differ from their install):")
        for d in computed.drifts:
            print(f"  {d.summary()}")

    problems = check.check_all(computed.buildable)
    if problems:
        print()
        sys.stdout.flush()
        tty.warn(f"{len(problems)} recipe constraint(s) not satisfied by the graph:")
        for p in problems:
            print(f"    {p}")

    stuck = graph.unshadowable(root, dev)
    if stuck:
        print()
        sys.stdout.flush()  # keep tty's stderr warning in order with our stdout
        tty.warn(
            "these installed packages link a dev package directly and will NOT see "
            "your changes (their DT_RPATH beats LD_LIBRARY_PATH):"
        )
        for name in sorted(stuck):
            print(f"    {name} <- {', '.join(sorted(stuck[name]))}")


# -- view ------------------------------------------------------------------


def splice_view_setup_parser(subparser):
    """(re)build the dev_view symlink farm

    The dev_view merges every installed package below the dev set into one
    directory tree, so builds see a single prefix instead of ~200 store paths.
    """
    subparser.add_argument(
        "--strict",
        action="store_true",
        help="fail on file conflicts instead of keeping the first of each",
    )
    _dir_arg(subparser)


def splice_view(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    if not st.picks:
        tty.die("nothing being developed yet. Use 'spack splice add <package>'.")

    # Build the dev specs first. Recipe drift can add or drop dependencies, so the
    # frontier has to be read off the *reconciled* specs rather than the pristine
    # base DAG -- otherwise a newly added dependency would be missing from the view.
    computed = specbuild.compute(st, root, weave_dag=False)
    specs = graph.view_closure(root, specbuild.frontier_of(computed.buildable))
    view.regenerate(st, specs, strict=args.strict)


# -- spec ------------------------------------------------------------------


def splice_spec_setup_parser(subparser):
    """weave the dev builds into the base spec and cache the result

    Constructs a concrete Spec for each dev package by copying the installed one
    and pointing it at your sources, then splices them in bottom-up. No
    concretization happens at any point.
    """
    subparser.add_argument("--json", action="store_true", help="dump the spliced spec as JSON")
    _dir_arg(subparser)


def splice_spec(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    if not st.picks:
        tty.die("nothing being developed yet. Use 'spack splice add <package>'.")

    started = time.time()
    computed = specbuild.compute(st, root)
    spliced, prefixes = computed.spliced, computed.prefixes
    specbuild.save(st, spliced)
    elapsed = time.time() - started

    if args.json:
        print(spliced.to_json(hash=spack.hash_types.dag_hash))
        return

    print(f"base    : {root.format('{name}{@version}{/hash:7}')}")
    print(f"spliced : {spliced.format('{name}{@version}{/hash:7}')}")
    print(f"\ndev prefixes ({len(prefixes)}) -- compiled from source:")
    for name in sorted(prefixes):
        print(f"  {name:<24} {prefixes[name]}")

    # Nodes splice rewired but that we are not rebuilding: they got a new hash
    # because a dependency changed, yet their binaries are the installed ones.
    rewired = sorted(
        s.name for s in spliced.traverse() if s.build_spec is not s and s.name not in prefixes
    )
    if rewired:
        print(f"\nrewired, not rebuilt ({len(rewired)}) -- keep their installed binaries:")
        print("  " + ", ".join(rewired))

    drift = specbuild.check_abi_preserved(root, spliced)
    if drift:
        sys.stdout.flush()
        tty.warn(f"compiler/runtime nodes changed identity: {', '.join(drift)}")
    else:
        print(f"\nABI: compiler and runtime nodes unchanged ({len(specbuild.abi_nodes(root))} nodes)")

    print(f"cached  : {st.spliced_file}")
    print(f"took    : {elapsed:.2f}s (no concretization)")


# -- src -------------------------------------------------------------------


def splice_src_setup_parser(subparser):
    """fetch sources for dev packages that don't have any yet

    Packages you picked with an explicit --path are left alone; this only fills in
    the ones defaulting to <dev-area>/src/<package>.
    """
    subparser.add_argument("packages", nargs="*", help="limit to these packages")
    _dir_arg(subparser)


def splice_src(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    computed = specbuild.compute(st, root, weave_dag=False)

    wanted = set(args.packages) if args.packages else None
    for name in computed.order:
        if wanted and name not in wanted:
            continue
        dest = st.picks[name]["path"] if name in st.picks else os.path.join(st.source_root, name)
        # Fetch using a spec *without* dev_path: that variant turns pkg.stage into a
        # DevelopStage, which has no fetcher to steal from.
        node = root[name] if name in graph.names_in(root) else computed.buildable[name]
        build.fetch_source(specbuild.without_dev_path(node), dest)


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


# -- env -------------------------------------------------------------------


def splice_env_setup_parser(subparser):
    """emit the runtime environment with the dev builds in front

    Takes the installed base spec's environment and prepends the dev prefixes, so
    the dev libraries shadow the installed ones. Intended to be eval'd:

        eval "$(spack splice env)"
    """
    fmt = subparser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--sh", action="store_const", dest="shell", const="sh", help="POSIX shell (default)"
    )
    fmt.add_argument("--csh", action="store_const", dest="shell", const="csh", help="C shell")
    fmt.add_argument("--fish", action="store_const", dest="shell", const="fish", help="fish")
    fmt.add_argument("--json", action="store_true", help="dump the resulting environment")
    fmt.add_argument(
        "--summary", action="store_true", help="show which prefix leads each search path"
    )
    _dir_arg(subparser)


def splice_env(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    if not st.picks:
        tty.die("nothing being developed yet. Use 'spack splice add <package>'.")

    computed = specbuild.compute(st, root, weave_dag=False)
    buildable, prefixes = computed.buildable, computed.prefixes
    present = runtime.built(prefixes)

    if not present:
        tty.die("no dev packages have been built yet. Run 'spack splice build'.")
    if len(present) < len(prefixes):
        unbuilt = sorted(set(prefixes) - set(present))
        sys.stdout.flush()
        tty.warn(f"not built, so absent from the environment: {', '.join(unbuilt)}")

    env = runtime.modifications(st, root, [buildable[n] for n in sorted(present)])

    if args.json:
        print(json.dumps(runtime.as_dict(env), indent=2, sort_keys=True))
    elif args.summary:
        print(f"dev packages in front ({len(present)}): {', '.join(sorted(present))}")
        for key, first, owner in runtime.summary(env, present):
            tag = f"  <- {owner}" if owner else ""
            print(f"  {key:<18} {first}{tag}")
    else:
        print(runtime.shell_code(env, shell=args.shell or "sh"))


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


# -- graph -----------------------------------------------------------------


def splice_graph_setup_parser(subparser):
    """print the dev subgraph as text or dot"""
    subparser.add_argument("--dot", action="store_true", help="emit graphviz dot")
    _dir_arg(subparser)


def splice_graph(args):
    st = state.find(args.dir)
    root = base.rehydrate(st)
    existing = [n for n, p in st.picks.items() if not p.get("new")]
    picks = graph.resolve_picks(root, existing)
    dev = graph.dev_set(root, picks.values())
    nodes, children, _ = graph.adjacency(root, graph.PROPAGATE)
    front = graph.frontier(root, dev)

    if not args.dot:
        for h in sorted(dev, key=lambda x: nodes[x].name):
            deps = sorted(nodes[c].name for c in children[h] & dev)
            print(f"{nodes[h].name} -> {', '.join(deps) if deps else '(frontier only)'}")
        return

    print("digraph splice {")
    print('  rankdir=LR; node [shape=box, style=filled, fillcolor="#e8e8e8"];')
    for h in dev:
        colour = "#ffd580" if h in set(picks.values()) else "#cfe8ff"
        print(f'  "{nodes[h].name}" [fillcolor="{colour}"];')
    for h in dev:
        for c in children[h] & dev:
            print(f'  "{nodes[h].name}" -> "{nodes[c].name}";')
        for c in children[h] & front:
            print(f'  "{nodes[h].name}" -> "{nodes[c].name}" [style=dashed, color=gray];')
    print("}")


# -- entry point -----------------------------------------------------------


def splice(parser, args):
    _dispatch[args.splice_command](args)
