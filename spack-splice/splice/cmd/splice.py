"""``spack splice`` -- develop part of an installed spec without concretizing."""

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
import yaml

import spack.cmd
import spack.error
import spack.hash_types
import spack.repo
import spack.util.git
import spack.util.tty as tty
from spack.util.tty.colify import colify

from spack.extensions.splice import (
    base,
    build,
    check,
    graph,
    pack,
    runtime,
    shadow,
    specbuild,
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
    ("shell",),
    ("pack",),
]

_dispatch = {}

#: The dev area on disk. Their presence is how a dev area is told from any old
#: directory, so ``init`` creates them and every other command insists on them.
SPLICE_YAML = "splice.yaml"
SUBDIRS = ("build", "install", "fetched_srcs")

def _get_packages(cfg):
    dev_packages = cfg.get("packages") or {}
    if not dev_packages:
        tty.die("nothing being developed yet. Use 'spack splice add <package>'.")
    return dev_packages

def read_area(path: str) -> dict:
    """Load ``splice.yaml`` from the dev area at ``path``, checking it is one."""
    area = Path(path)
    missing = [n for n in (SPLICE_YAML, *SUBDIRS) if not (area / n).exists()]
    if missing:
        tty.die(
            f"{path} is not a splice dev area: no {', '.join(missing)}",
            "Run 'spack splice init' there first.",
        )

    with open(area / SPLICE_YAML) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data.get("splice"), dict) or "hash" not in data["splice"]:
        tty.die(f"{area / SPLICE_YAML} is not readable as splice state")
    return data


def write_area(path: str, data: dict) -> None:
    with open(Path(path, SPLICE_YAML), "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


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
    for dp in [Path(args.dir, n) for n in SUBDIRS]:
        try:
            dp.mkdir(parents=True, exist_ok=False)
        except:
            tty.die(f'{args.dir} already contains {dp}')

    try:
        Path(args.dir, SPLICE_YAML).touch(exist_ok=False)
    except:
        tty.die(f'{args.dir} already contains {SPLICE_YAML}')


    # Record what it resolved *to*, not just what was typed: 'args.spec' may be
    # None or a hash prefix, and --env may be a name that later points elsewhere.
    # 'environment' is the resolved directory (holding spack.yaml and spack.lock),
    # or null when the base came from the store.
    write_area(args.dir, {
        'splice': {
            'spec': spec.format('{name}{@version}'),
            'hash': spec.dag_hash(),
            'environment': base.env_of(source),
            'packages': {},
        }
    })

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
    """choose package within the base spec to develop locally
    """
    subparser.add_argument("package", help="package name within the base spec")
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )
    subparser.add_argument(
        "-s", "--src", default=None,
        type=str,
        help="Source code for the package." \
            " If none provided, attempt to grab from git using the git repo defined in package.py." \
            " If the git repo is not defined within package.py, an empty directory is made in fetched_srcs/<package>" \
            " If a git repo url is provided. The repo is cloned down within fetched_srcs/<package>" \
            " Otherwise, a path may be provided."
    )
    subparser.add_argument(
        "--allow-unshadowed",
        action="store_true",
        help="add the package even though installed packages above it were built "
        "with RPATH and so will not see the dev build",
    )

def _occupied(dest: Path) -> bool:
    """Whether something is already sitting at the clone target."""
    if not dest.exists():
        return False
    if not dest.is_dir():
        tty.die(f"{dest} exists and is not a directory")
    return any(dest.iterdir())


def _git_ask(dest: Path, *args):
    """Run a read-only git query in ``dest``, or None if it isn't a checkout."""
    git = spack.util.git.git()
    if git is None or not (dest / ".git").exists():
        return None
    out = git("-C", str(dest), *args, output=str, error=str, fail_on_error=False)
    return out.strip() if git.returncode == 0 and out else None


def _git_origin(dest: Path):
    return _git_ask(dest, "remote", "get-url", "origin")


def _git_note(dest: Path):
    """``origin`` and ``describe`` for an existing checkout, for reporting only."""
    parts = (_git_origin(dest), _git_ask(dest, "describe", "--tags", "--always", "--dirty"))
    return ", ".join(x for x in parts if x) or None


def _same_remote(a: str, b: str) -> bool:
    return a.rstrip("/").removesuffix(".git") == b.rstrip("/").removesuffix(".git")


def _adopt(dest: Path) -> Path:
    """Reuse sources already sitting at the clone target.

    Mostly reached by re-adding something that was ``rm``ed, since ``rm`` leaves the
    checkout alone. Nothing is fetched and nothing is checked out: the working tree
    may hold uncommitted work, and touching it would be a good way to lose it.
    """
    note = _git_note(dest)
    tty.msg(f"reusing the sources already at {dest}" + (f" ({note})" if note else ""))
    return dest


def _clone(url: str, dest: Path) -> Path:
    git = spack.util.git.git(required=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tty.msg(f"cloning {url}")
    sys.stdout.flush()  # git writes its progress to stderr; keep the two in order
    git("clone", url, str(dest))
    return dest


def _recipe_ref(pkg_cls, version):
    """The git ref a recipe pins ``version`` to, if it pins one.

    Usually it does not: most recipes fetch a tarball and record only a sha256,
    which says nothing about where the tag lives in the repository.
    """
    for candidate, info in pkg_cls.versions.items():
        if str(candidate) == str(version):
            return next((info[k] for k in ("commit", "tag", "branch") if info.get(k)), None)
    return None


def _checkout_version(dest: Path, package, pkg_cls, version) -> None:
    """Move the clone onto whatever matches the installed version.

    Worth the effort: splice builds this source *against* the installed graph, so
    starting from the default branch instead of the version that was actually
    installed is a silent way to get an incompatible build.

    The recipe rarely says which ref a version is, so the common tag spellings get
    tried in turn -- '2.13.0', 'v2.13.0', 'hwloc-2.13.0'.
    """
    git = spack.util.git.git(required=True)
    candidates = (
        _recipe_ref(pkg_cls, version),
        str(version),
        f"v{version}",
        f"{package}-{version}",
    )
    for ref in candidates:
        if not ref:
            continue
        # Captured, not printed: a ref that does not exist is an expected miss here.
        git(
            "-C", str(dest), "checkout", "--quiet", ref,
            output=str, error=str, fail_on_error=False,
        )
        if git.returncode == 0:
            tty.msg(f"checked out {ref}")
            return
    tty.warn(
        f"no ref matching {version} in the repository, so the clone is on its "
        "default branch. It does not match the installed build -- check out the "
        "right version yourself before building."
    )


def _attempt_fetch_from_package(package, version, dest: Path) -> Path:
    """Clone the git repository the recipe declares, if it declares one."""
    if _occupied(dest):
        return _adopt(dest)

    try:
        pkg_cls = spack.repo.PATH.get_pkg_class(package)
    except Exception as e:  # noqa: BLE001 -- a missing recipe is not fatal here
        tty.debug(f"no recipe for {package}: {e}")
        pkg_cls = None

    url = getattr(pkg_cls, "git", None) if pkg_cls else None
    if not url:
        dest.mkdir(parents=True, exist_ok=True)
        tty.warn(
            f"{package}'s recipe declares no 'git' url, so {dest} is empty. "
            "Add one to package.py, or re-add with --src.",
        )
        return dest

    _clone(url, dest)
    _checkout_version(dest, package, pkg_cls, version)
    return dest


def _attempt_fetch_from_uri(uri, dest: Path) -> Path:
    """Clone whatever the user pointed at, as-is. No version is assumed.

    Adopts an existing checkout only when it came from the same place. Naming a
    url is a statement of intent, so silently keeping sources from somewhere else
    would be answering a different question than the one asked.
    """
    if _occupied(dest):
        origin = _git_origin(dest)
        if origin and _same_remote(origin, uri):
            return _adopt(dest)
        tty.die(
            f"{dest} already holds sources" + (f" from {origin}" if origin else "")
            + f", which is not {uri}",
            "Remove it, or drop --src to develop what is already there.",
        )
    return _clone(uri, dest)


def _is_uri(input_string):
    parsed = urlparse(input_string)
    # A URI must have a scheme (e.g., http, https, file, ftp)
    # Windows drive letters (like C:) might misparse as schemes, so we ensure length > 1
    if parsed.scheme and len(parsed.scheme) > 1:
        return parsed
    # scp-style git remotes ('git@host:org/repo.git') carry no scheme at all, but
    # are still remotes rather than paths.
    if re.match(r"^[\w.-]+@[\w.-]+:", input_string):
        return input_string
    return None


def _resolve_src(package, version, src, dest: Path) -> Path:
    if src is None:
        return _attempt_fetch_from_package(package, version, dest)
    if _is_uri(src) is not None:
        return _attempt_fetch_from_uri(src, dest)

    try:
        return Path(src).resolve(strict=True)  # Must already exist
    except OSError:
        tty.die(f"--src path does not exist: {src}")


def _describe_blocked(blocked) -> list:
    """One line per dependent that will not see a dev build."""
    rpath = [s for s, verdict in blocked if verdict is False]
    unknown = [s for s, verdict in blocked if verdict is None]
    return [s.format("{name}{@version}{/hash:7}") for s in rpath] + [
        f"{s.format('{name}{@version}')} (could not be inspected)" for s in unknown
    ]


def _require_shadowable(root, package, developed, allow: bool) -> None:
    """Refuse to develop a package nothing above it can see.

    The dev build reaches its dependents through LD_LIBRARY_PATH, which DT_RPATH
    overrides. A dependent installed with RPATH keeps the library it was built
    against, so developing under it changes nothing at run time -- a failure that
    looks like "my edits do nothing" rather than anything to do with linking. Better
    to say so now than after a build.
    """
    blocked = shadow.blocked_dependents(root, package, developed)
    if not blocked:
        return

    detail = _describe_blocked(blocked)

    if allow:
        sys.stdout.flush()
        tty.warn(
            f"{len(blocked)} installed package(s) above {package} will not see the "
            "dev build, and --allow-unshadowed was given:",
            *detail,
        )
        return

    tty.die(
        f"{len(blocked)} installed package(s) link {package} directly and cannot be "
        "redirected to a dev build (DT_RPATH beats LD_LIBRARY_PATH):",
        *detail,
        f"Develop them too ('spack splice add <name>'), reinstall them with "
        "'shared_linking: runpath', or pass --allow-unshadowed to proceed anyway.",
    )


def splice_add(args):
    data = read_area(args.dir)
    cfg = data["splice"]

    # The base has to still be there, and still be installed: an environment can be
    # re-concretized and a spec uninstalled long after 'init' recorded them.
    root = base.reload(cfg["hash"], cfg.get("environment"))

    # Everything named must already be in the base spec. Adding a package that is
    # not there would mean concretizing, which splice never does.
    picks = graph.resolve_picks(root, [args.package])

    # Before fetching anything: if the packages above this one cannot see a dev
    # build, developing it achieves nothing at run time.
    _require_shadowable(root, args.package, cfg.get("packages") or {}, args.allow_unshadowed)

    nodes, _, _ = graph.adjacency(root, graph.PROPAGATE)
    packages = cfg.get("packages") or {}

    # TODO -- just do a simple if-else check
    already = sorted(set(picks) & set(packages))
    added = sorted(set(picks) - set(packages))

    # Fetch before recording: a clone that fails should leave splice.yaml untouched
    # rather than pointing at a directory that was never populated.
    for name in added:
        src = _resolve_src(
            name,
            nodes[picks[name]].version,
            args.src,
            Path(args.dir, "fetched_srcs", name).resolve(),
        )
        packages[name] = {"path": str(src)}

    cfg["packages"] = packages
    write_area(args.dir, data)

    if already:
        tty.msg(f"already being developed, left alone: {', '.join(already)}")
    if added:
        tty.msg(f"now developing: {', '.join(added)}")
    _report_dev_set(args.dir, root, packages)


def _report_dev_set(area, root, packages):
    """List exactly what will be rebuilt -- which is only what the user chose.

    Deliberately *not* the interval closure of the picks. The base stack is linked
    with DT_RUNPATH, which LD_LIBRARY_PATH overrides, so an untouched package
    sitting between two dev packages still loads the dev build at run time and does
    not need rebuilding. Only a dependent that cannot be shadowed would, and that
    is a property of its ELF tags rather than of its position in the graph.
    """
    nodes, _, _ = graph.adjacency(root, graph.PROPAGATE)
    by_hash = graph.resolve_picks(root, list(packages))

    print()
    print(f"developing {len(packages)} of {len(nodes)} packages in {root.name}:")
    for name in sorted(packages):
        spec = nodes[by_hash[name]]
        src = os.path.relpath(packages[name]["path"], os.path.abspath(area))
        print(f"  {spec.name}@{spec.version}  <- {src}")


def splice_rm_setup_parser(subparser):
    """stop developing packages

    The fetched sources are left where they are, since they may hold work that is
    not committed anywhere. Remove them yourself if you want them gone.
    """
    subparser.add_argument("packages", nargs="+", help="package names to unpick")
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )
    subparser.add_argument(
        "--allow-unshadowed",
        action="store_true",
        help="remove the package even though doing so strands dev packages beneath it",
    )


def _require_remaining_shadowable(root, remaining, removing, allow: bool) -> None:
    """Refuse a removal that strands whatever is left in the dev set.

    Dropping a package turns it back into an installed binary, and if that binary
    carries RPATH then the dev packages *below* it stop being visible -- the same
    condition ``add`` refuses, arrived at from the other direction.
    """
    stranded = [
        (package, blocked)
        for package in sorted(remaining)
        for blocked in [shadow.blocked_dependents(root, package, remaining)]
        if blocked
    ]
    if not stranded:
        return

    detail = [
        f"{package} <- {line}"
        for package, blocked in stranded
        for line in _describe_blocked(blocked)
    ]
    removed = ", ".join(removing)

    if allow:
        sys.stdout.flush()
        tty.warn(
            f"removing {removed} strands {len(stranded)} dev package(s), and "
            "--allow-unshadowed was given:",
            *detail,
        )
        return

    tty.die(
        f"removing {removed} would leave {len(stranded)} dev package(s) with "
        "installed dependents that cannot see them:",
        *detail,
        "Remove those as well, or pass --allow-unshadowed.",
    )


def splice_rm(args):
    data = read_area(args.dir)
    cfg = data["splice"]
    dev_packages = cfg.get("packages") or {}

    unknown = [p for p in args.packages if p not in dev_packages]
    if unknown:
        tty.die(f"not currently developed: {', '.join(unknown)}")

    remaining = {n: v for n, v in dev_packages.items() if n not in args.packages}
    if remaining:
        # Advisory only: a dev area whose base has gone must still be prunable, so a
        # base that will not load costs the check rather than the removal.
        try:
            root = base.reload(cfg["hash"], cfg.get("environment"))
        except spack.error.SpackError as e:
            tty.warn(f"cannot check what this strands: {e}")
        else:
            _require_remaining_shadowable(root, remaining, args.packages, args.allow_unshadowed)

    dropped = [dev_packages[name]["path"] for name in args.packages]
    cfg["packages"] = remaining
    write_area(args.dir, data)

    tty.msg(f"no longer developing: {', '.join(args.packages)}")
    for path in dropped:
        tty.msg(f"sources left in place: {path}")


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
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )


def splice_build(args):
    data = read_area(args.dir)
    cfg = data["splice"]
    dev_packages = _get_packages(cfg)

    root = base.reload(cfg["hash"], cfg.get("environment"))

    # An empty directory is the usual case here: 'add' makes one when the recipe
    # declares no git url. Catching it now beats failing deep inside cmake.
    missing = sorted(
        name
        for name, entry in dev_packages.items()
        if not os.path.isdir(entry["path"]) or not os.listdir(entry["path"])
    )
    if missing:
        tty.die(
            f"no sources for {', '.join(missing)}",
            "Put them in place, or re-add with "
            "'spack splice add <package> --src <path-or-url>'.",
        )

    # Recompute rather than reading the cache: dev prefixes are assigned in memory
    # by Spec.set_prefix and are not part of the serialized spec. The splice is
    # skipped -- we build the hand-built specs, which still have their build deps.
    area = os.path.abspath(args.dir)
    computed = specbuild.compute(root, dev_packages, area, weave_dag=False)
    buildable, prefixes = computed.buildable, computed.prefixes

    order = list(computed.order)
    if args.packages:
        unknown = [p for p in args.packages if p not in order]
        if unknown:
            tty.die(f"not in the dev set: {', '.join(unknown)}")
        order = [n for n in order if n in set(args.packages)]

    for d in computed.drifts:
        tty.warn(f"recipe drift: {d.summary()}")

    # Stages, and so the build trees, live in the dev area rather than $tempdir:
    # a failed build is something you go and read, and it should die with the area.
    with build.staged_in(os.path.join(area, "build")):
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


# -- setup -----------------------------------------------------------------


#: Dialects ``EnvironmentModifications.shell_modifications`` can emit.
SHELLS = ("sh", "csh", "fish", "bat", "pwsh")


def splice_shell_setup_parser(subparser):
    """set up the run environment for a dev area's packages

    The run environment each package.py declares, with the dev builds in front of
    the installed stack: the recipes' own variables come from the dev prefixes, and
    LD_LIBRARY_PATH points at their lib directories so the dev libraries shadow the
    installed ones.

    Nothing can change the environment of the shell that invoked it, so by default
    this prints the commands to do so and you apply them yourself:

        eval "$(spack splice shell)"

    Pass --subshell to start a new shell with all of it applied instead, which
    leaves the current one untouched and ends when you exit.
    """
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )
    subparser.add_argument(
        "-s",
        "--subshell",
        action="store_true",
        help="start a new shell with the environment applied, rather than printing it",
    )
    dialect = subparser.add_mutually_exclusive_group()
    for name in SHELLS:
        dialect.add_argument(
            f"--{name}",
            action="store_const",
            dest="shell",
            const=name,
            help=f"print {name} commands (default: guessed from $SHELL)",
        )
    subparser.set_defaults(shell=None)


def _shell_dialect(chosen):
    """Which dialect to print, from the flag or from ``$SHELL``."""
    if chosen:
        return chosen
    name = os.path.basename(os.environ.get("SHELL", "")).lower()
    if name in ("csh", "tcsh"):
        return "csh"
    if name == "fish":
        return "fish"
    return "sh"


def _dev_environment(area, cfg):
    """The base stack's environment with the built dev packages layered in front.

    Only *built* packages take part: an unbuilt one has no prefix on disk, and
    putting a nonexistent directory on LD_LIBRARY_PATH would quietly do nothing.
    """
    packages = _get_packages(cfg)


    root = base.reload(cfg["hash"], cfg.get("environment"))
    computed = specbuild.compute(root, packages, area, weave_dag=False)

    present = runtime.built(computed.prefixes)
    if not present:
        tty.die("no dev packages have been built yet. Run 'spack splice build'.")

    unbuilt = sorted(set(computed.prefixes) - set(present))
    if unbuilt:
        # stderr, so it cannot end up inside an eval
        tty.warn(f"not built, so absent from the environment: {', '.join(unbuilt)}")

    env = runtime.modifications(area, root, [computed.buildable[n] for n in sorted(present)])
    return env, present


#: Set inside a subshell so the prompt code, and a second invocation, can see it.
SPLICE_SHELL_VAR = "SPLICE_SHELL"


def _subshell_argv(shell: str, label: str):
    """``(argv, env overrides)`` for a subshell whose prompt announces ``label``.

    Exporting PS1 does not work: bash and zsh read their startup files after exec
    and set the prompt unconditionally, overwriting anything inherited. The prompt
    has to be amended *after* those files run, which each shell offers a hook for --
    ``--rcfile`` for bash, ``$ZDOTDIR`` for zsh -- so a small rc file is generated
    that sources the user's own and then prefixes the prompt.

    The rc file removes its own directory once read, so nothing is left behind. Any
    other shell gets no prompt change: it still gets ``SPLICE_SHELL`` in the
    environment to key its own prompt off.
    """
    name = os.path.basename(shell)
    if name not in ("bash", "zsh"):
        return [shell], {}

    tmp = tempfile.mkdtemp(prefix="splice-shell-")
    quoted_tmp = shlex.quote(tmp)
    prefix = shlex.quote(f"({label}) ")

    if name == "bash":
        rc = os.path.join(tmp, "bashrc")
        script = (
            "# Generated by 'spack splice shell --subshell'.\n"
            'if [ -f "$HOME/.bashrc" ]; then . "$HOME/.bashrc"; fi\n'
            f"PS1={prefix}$PS1\n"
            f"rm -rf {quoted_tmp}\n"
        )
        argv = [shell, "--rcfile", rc, "-i"]
        overrides = {}
    else:
        rc = os.path.join(tmp, ".zshrc")
        # Hand ZDOTDIR back before sourcing, so the user's own startup files -- and
        # anything they launch later -- look in the usual place.
        script = (
            "# Generated by 'spack splice shell --subshell'.\n"
            'ZDOTDIR="${SPLICE_ZDOTDIR:-$HOME}"\n'
            "export ZDOTDIR\n"
            'if [ -f "$ZDOTDIR/.zshrc" ]; then . "$ZDOTDIR/.zshrc"; fi\n'
            f"PROMPT={prefix}$PROMPT\n"
            f"rm -rf {quoted_tmp}\n"
        )
        argv = [shell, "-i"]
        overrides = {
            "ZDOTDIR": tmp,
            "SPLICE_ZDOTDIR": os.environ.get("ZDOTDIR", os.path.expanduser("~")),
        }

    with open(rc, "w") as f:
        f.write(script)
    return argv, overrides


def splice_shell(args):
    area = os.path.abspath(args.dir)
    cfg = read_area(args.dir)["splice"]
    env, present = _dev_environment(area, cfg)

    if not args.subshell:
        # Only the shell code goes to stdout: anything else would be eval'd.
        print(runtime.shell_code(env, shell=_shell_dialect(args.shell)))
        if sys.stdout.isatty():
            tty.warn(
                "that was printed, not applied -- a command cannot change the shell "
                "that ran it.",
                'Use: eval "$(spack splice shell)", or --subshell for a new shell.',
            )
        return

    shell = os.environ.get("SHELL") or "/bin/sh"
    label = f"splice:{os.path.basename(area.rstrip(os.sep))}"

    if os.environ.get(SPLICE_SHELL_VAR):
        tty.warn(
            f"already inside {os.environ[SPLICE_SHELL_VAR]}; this nests another shell "
            "rather than replacing it."
        )

    resolved = runtime.as_dict(env)
    resolved[SPLICE_SHELL_VAR] = label
    argv, overrides = _subshell_argv(shell, label)
    resolved.update(overrides)

    tty.msg(
        f"entering ({label}) with {len(present)} dev package(s) in front: "
        f"{', '.join(sorted(present))}",
        f"dev area {area}; 'exit' to leave",
    )
    sys.stdout.flush()
    os.execve(shell, argv, resolved)

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
    subparser.add_argument(
        "-d", "--dir", default=".", help="directory for the dev area (default: cwd)"
    )


def splice_pack(args):
    area = os.path.abspath(args.dir)
    cfg = read_area(args.dir)["splice"]
    dev_packages = _get_packages(cfg)

    root = base.reload(cfg["hash"], cfg.get("environment"))
    computed = specbuild.compute(root, dev_packages, area)
    spliced, buildable, prefixes = computed.spliced, computed.buildable, computed.prefixes
    present = runtime.built(prefixes)
    if not present:
        tty.die("no dev packages have been built yet. Run 'spack splice build'.")
    if len(present) < len(prefixes):
        unbuilt = sorted(set(prefixes) - set(present))
        sys.stdout.flush()
        tty.warn(f"not built, so absent from the tarball: {', '.join(unbuilt)}")

    env = runtime.modifications(area, root, [buildable[n] for n in sorted(present)])
    # Resolve against an *empty* environment, not os.environ: we want only what Spack
    # contributes, so this machine's PATH does not get baked into a portable artifact.
    resolved = {}
    env.apply_modifications(resolved)
    # ...and remember which variables are search paths, so the generated script can
    # extend the target's values instead of replacing them.
    path_vars = runtime.path_variables(env)

    output = args.output or os.path.join(os.getcwd(), os.path.basename(area) + ".tar.gz")
    path, digest, packed = pack.create(
        area, cfg, present, resolved, path_vars, root, output, spliced=spliced
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
