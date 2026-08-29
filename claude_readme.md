# spack-splice

A Spack extension for developing part of a large, already-installed stack **without
ever running the concretizer**.

Rebuilding one package in a 200+ node art/LArSoft/DUNE stack normally means
re-concretizing the whole thing. Spack's own `spack develop` and `spack dev-build`
both call `spack.concretize.concretize_one`, so neither avoids the solver. When you
just want to edit a file and rebuild, the solver dominates the turnaround.

`splice` starts from a spec that is already concrete and already installed, and only
ever *reads* that spec. You pick packages to develop; splice derives the rest of the
rebuild set from the existing DAG, symlinks everything below it into a `dev_view`, and
builds through your own recipe repos.

On a real 216-node `art-suite` spec, the whole graph analysis takes **~0.3 s**.

## About the name

Spack already uses "splice" for ABI substitution during concretization: `Spec.splice()`,
the `concretizer:splice:*` config, the `can_splice` directive, `spack.rewiring`. This
extension is a different thing (overlaying local dev builds onto an installed spec) but
it is *built on* `Spec.splice()`, so the overlap is real rather than accidental.

## Install

```console
$ spack config add "config:extensions:[/path/to/spack-splice-prototype/spack-splice]"
$ spack splice --help
```

## Use

```console
$ mkdir mydev && cd mydev
$ spack splice init art-suite
$ spack splice add cetlib art
$ spack splice status
```

The base can be given several ways — all read, none concretized:

```console
$ spack splice init art-suite            # installed spec, or /hash
$ spack splice init myenv                # name of a managed environment
$ spack splice init ../some/env          # environment directory
$ spack splice init /path/to/spec.json   # a spec file
$ spack -e myenv splice init             # omit it: use the active environment
```

If a name is both an environment and an installed package, the environment wins and
splice says so; pass a version or `/hash` to mean the package.

An environment with more than one root needs `--root` to say which to develop
against — by name, by spec, or by hash:

```console
$ spack splice init multiroot
==> Error: environment 'multiroot' has 2 roots:
  art-suite@s134/yicajn7
  cetlib-except@1.09.01/qcuzmxa
Choose one with --root, e.g. --root art-suite or --root /yicajn7

$ spack splice init multiroot --root cetlib-except
$ spack splice init multiroot --root cetlib-except@1.09.01
$ spack splice init multiroot --root /yicajn7
```

Only `init` takes `--root`. The chosen root's hash goes into the dev area's state, so
every later command reselects it automatically.

### `init` in full: three independent axes

`init` has three options and two of them take spec-looking strings, which is the whole
source of the confusion:

| flag | question it answers | default |
|---|---|---|
| `-d`, `--dir` | *where does the dev area go on disk?* | `.` (cwd) |
| positional `spec` | *which concrete spec is frozen as the base?* | the active environment |
| `--root` | *which root **inside** that environment?* | only needed if >1 root |

`--root` is not the root directory — that is `-d`. And `--root` only means anything when
the base is an environment; against an installed spec it is a hard error rather than a
silent no-op (`base.py:210`).

The positional argument is not typed by a flag; `resolve` (`base.py:177`) tries these in
order and takes the first that matches:

| what you pass | read as | where |
|---|---|---|
| *nothing* | the active environment | `base.py:187` |
| a directory holding `spack.yaml` | environment directory | `base.py:197` |
| a path to an existing file | raw `spec.json` / `spack.lock` | `base.py:199` |
| a managed environment name | environment by name | `base.py:202` |
| anything else | store lookup: `cetlib-except`, `cetlib-except@1.09.01`, `/chbb6nm` | `base.py:214` |

Order is why the collision rule above falls out: environments are consulted before the
store, so an ambiguous name resolves to the environment and says so. A store lookup that
matches two specs is an error listing both, never a guess.

`--root` matches either as a **dag-hash prefix** (`/pyq7lul`) or as a spec through
`satisfies` (`dunesw`, `dunesw@10.22.00d00`, `dunesw+cuda`) — `matching_roots`,
`base.py:83`. Zero matches lists the roots; more than one asks you to narrow it.

How many roots an environment has is not a small number in practice. The DUNE
prototype environments carry **75 concrete roots** — `dunesw`, `dunereco`,
`duneexamples`, `art`, `cetlib-except`, and non-HEP leaves like `awscli` and
`snakemake` — so `--root` is effectively mandatory there:

```console
$ spack splice init dunesw-10_22_00d00-justin-01_06_01-prototype --root dunesw -d dev_area/
==> dev area at .../dev_area
  base: dunesw@10.22.00d00/lqtyvrb (361 nodes)
  arch: linux-almalinux9-x86_64_v3
  from: env:/.../environments/dunesw-10_22_00d00-justin-01_06_01-prototype
```

### What init records, and what it therefore survives

The state file keeps only `base_hash` and `base_source`, the latter being one of
`env:<resolved dir>`, `file:<path>` or `store:<query>`. Three consequences:

* For an environment the **resolved directory** is recorded, not the name you typed
  (`base.py:105`), so re-pointing that name later cannot silently move the dev area.
* `rehydrate` (`base.py:217`) reselects the root by recorded **hash**, which is why
  `--root` is given once at init and never again — a multi-root environment would
  otherwise demand it on every single command.
* If the source is re-concretized underneath you, every later command warns: `now
  resolves to X, but this dev area was created against Y`. A store base that has since
  been uninstalled says `the dev area is stale. Re-run 'spack splice init'`.

`init` also does two quiet things worth knowing. It re-reads installed specs from
`<prefix>/.spack/spec.json` instead of trusting the database (see *The database record
is lossy*), and it warns when the base's OS differs from the host — which matters
because Spack's install layout has no OS component, so one store can hold specs from
several containers with nothing in the path to tell them apart.

```
dev set (5 packages to rebuild):
  * art@3.14.04
    canvas@3.17.00
  * cetlib@3.18.02
    fhicl-cpp@4.18.04
    messagefacility@2.10.05
  (* = chosen by you, the rest are implied)

frontier : 19 direct deps -> 44 packages in the dev_view
```

You picked `art` and `cetlib`; splice worked out that `canvas`, `fhicl-cpp` and
`messagefacility` sit on dependency paths between them and must be rebuilt too.

## How the dev set is chosen

Given picks `P` inside the base DAG, the dev set is the **interval closure**:

```
D = (ancestors(P) ∩ descendants(P)) ∪ P
```

A node is rebuilt if it lies on a dependency path *between* two picked packages. For
`A → B → C → D` with picks `{A, C}`, the dev set is `{A, B, C}` — `B` is pulled in, `D`
is not. `D` stays installed and is symlinked into the dev_view.

Nothing *above* the topmost pick is rebuilt. That is deliberate, and it is what the
`LD_LIBRARY_PATH` shadowing is for.

### The shadowing caveat, stated plainly

Spack installs binaries with **DT_RPATH**, which the dynamic loader searches *before*
`LD_LIBRARY_PATH`. So an already-installed package that links a dev package **directly**
will keep using the installed library no matter what you put on `LD_LIBRARY_PATH`.

Shadowing does work for `dlopen`'d plugins (the art/LArSoft `CET_PLUGIN_PATH` model) and
for anything splice rebuilds, since dev builds are linked with `-Wl,--enable-new-dtags`
(DT_RUNPATH), which *does* lose to `LD_LIBRARY_PATH`.

`spack splice status` tells you exactly which packages fall in the gap:

```
==> Warning: these installed packages link a dev package directly and will NOT see
    your changes (their DT_RPATH beats LD_LIBRARY_PATH):
    cetlib <- art, art-root-io, art-suite, canvas, canvas-root-io, fhicl-cpp, messagefacility
```

If you need one of those to see your changes, add it to the dev set.

## The dev_view

```console
$ spack splice view
==> dev_view: 41 packages linked into .../devtest/view
```

Everything below the dev set is merged into one symlink farm, so a build sees a
single `-I`/`-L` root instead of ~200 store prefixes. `<dev>/view` is a symlink to a
content-hashed tree and is swapped atomically, so a failed regeneration leaves the
previous view untouched and an unchanged view is a no-op.

File conflicts are reported and then tolerated — first package wins, the same
default Spack's own environment views use. In a large closure they are essentially
always `LICENSE`, `README` and `share/info/dir`. Pass `--strict` to make them fatal.

## Splicing in the dev builds

```console
$ spack splice spec
base    : art-suite@s134/yicajn7
spliced : art-suite@s134/gr6plvo

dev prefixes (5) -- compiled from source:
  art                      <dev>/install/art-x5peufk
  ...

rewired, not rebuilt (3) -- keep their installed binaries:
  art-root-io, art-suite, canvas-root-io

ABI: compiler and runtime nodes unchanged (5 nodes)
took    : 2.72s (no concretization)
```

Each dev package's spec is *copied* from the installed node, given a `dev_path`
variant, and spliced in with `Spec.splice`. Nothing is solved for. Version, variants,
architecture, compiler, and the choice of every dependency build all come straight
from the installed spec — which is precisely why the result is ABI-compatible with
the parts of the stack that are not being rebuilt.

For comparison, `spack spec art-suite` on the same 216-node stack takes **22.7s**.

| stack | nodes | dev pkgs | `splice spec` |
|---|---|---|---|
| hep-concurrency | 10 | 2 | 0.04s |
| art-suite | 216 | 5 | 2.8s |
| dunesw | 319 | 4 | 8.5s |

### Splicing a spliced DAG compounds

Each splice is passed back through JSON before the next one. This is not tidiness —
without it the cost explodes. Measured on dunesw, splicing four packages in turn:

```
splice #1 dunecore     1.39s
splice #2 dunecalib   14.74s        # and #3 would have been minutes
```

A freshly deserialized spec carries explicit hashes for every node and none of the
partially-invalidated caches a splice leaves behind. Round-tripping between splices
flattens it to ~1.9s each — dunesw goes from effectively unbounded to 8.5s, and
art-suite is unchanged at 2.8s because the round-trip cost offsets the cheaper
splices.

The obvious suspect — `build_spec` chains deepening so the frankenhash at
`spec.py:2161` recurses — was measured and ruled out. There were no chains to
collapse.

### Two things that look like optimizations and are not

Both were tried, measured, and reverted:

* **`transitive=True`** on the splice. Reconciles using the dev subtree's copies and
  stampedes: 68 nodes rewired instead of 3, and `llvm` changed identity, breaking the
  ABI guarantee.
* **Building the dev subgraph by hand and splicing only its maximal nodes** (one
  splice instead of N). Much faster, but with `transitive=False` every dev package
  below the top silently reverts to its installed build — 4 of 5 on art-suite — and
  with `transitive=True` you get the stampede above.

So the cost is one full-DAG pass per dev package, and `verify_dev_nodes` fails loudly
if a dev package ever comes out of the splice without its `dev_path`. That guard
exists because the failure mode is silent: you get a dev area that rebuilds nothing.

## Building

```console
$ spack splice src            # fetch sources for dev packages that lack them
$ spack splice build -j8      # compile bottom-up into the dev area
```

Builds go through Spack's own builder — `setup_package` followed by the builder's
phases — rather than `PackageInstaller`, which would compute prefixes from the store
layout and register what it builds in the store database. `Spec.set_prefix` redirects
each dev package into `<dev-area>/install/<name>-<hash>`, so the shared install tree
is never touched. All recipe logic (`cmake_args`, patches, `setup_build_environment`,
cetmodules quirks) runs exactly as under `spack install`.

Every build is wrapped in a `config:shared_linking: {type: runpath}` override, which
flips the compiler wrapper's dtags:

| | `SPACK_DTAGS_TO_ADD` | result |
|---|---|---|
| Spack default | `--disable-new-dtags` | DT_RPATH |
| under splice | `--enable-new-dtags` | **DT_RUNPATH** |

DT_RUNPATH loses to `LD_LIBRARY_PATH`, which is what makes shadowing work. Nothing
global is changed and the installed tree keeps its rpaths. `splice build` checks the
resulting libraries and warns if anything comes out with RPATH.

Note it must be written as a dict. `("config:shared_linking:type", "runpath")` raises
`KeyError: 'shared_linking'` — `Configuration.set` does not create intermediate keys
(`config.py:935`).

### Build from what was built, not from what was spliced

`Spec.splice` strips build dependencies from every node it splices: in Spack's model
a spliced node is *rewired* from an existing binary and never compiled, so its build
deps live on `build_spec`. Measured on art-suite, `cetlib-except` enters the splice
with 8 dependencies and leaves with 2, having lost `gcc`, `ninja`, `cmake`,
`cetmodules` and `catch2`. Building from that gives `CMAKE_CXX_COMPILER not set`.

So splice keeps two artifacts: the woven DAG for reporting, and the hand-built dev
specs — which never go through splice and therefore keep their build deps — for
compiling. Restoring the edges after splicing would be worse: `dag_hash` covers build
deps (`hash_types.py:53`), so it would churn every hash in the DAG after splicing had
already settled.

### Verified end to end

Developing `cetlib-except` out of the 216-node `art-suite` stack on debian12:

```console
$ spack splice build -j8
==> cetlib-except: cmake
==> cetlib-except: build
==> cetlib-except: install
==> cetlib-except: RUNPATH (shadowing works)
```

Then edit `exception.cc` and rebuild — **0.81s** for the whole cycle, including
Spack startup, spec construction and the incremental ninja build. Concretizing the
same stack takes 22.7s before any compiling starts.

The build lands in `<dev-area>/install/`, `spack find` still shows only the
pre-existing installs, and the library comes out with DT_RUNPATH.

Shadowing, demonstrated against a consumer linked to the *installed* library:

```console
$ ./consumer                                 # DT_RUNPATH
---- Shadow BEGIN
$ LD_LIBRARY_PATH=<dev>/lib ./consumer
==SPLICE-DEV2= Shadow BEGIN                  # the dev build won
```

And the documented gap, same test with DT_RPATH:

```console
$ LD_LIBRARY_PATH=<dev>/lib ./consumer_rpath
---- Shadow BEGIN                            # still the installed library
```

The installed `art` binary behaves the same way — `ldd` resolves `libcetlib_except`
to the store no matter what `LD_LIBRARY_PATH` says. That is exactly the set
`splice status` warns about.

## The runtime environment

```console
$ eval "$(spack splice env)"
```

Composed from the **installed** base spec, with the dev prefixes prepended. The
installed spec is used because every node in it has a real prefix on disk, whereas
the spliced DAG contains rewired nodes whose hashes correspond to nothing that was
ever installed.

Dev prefixes go in front of `PATH`, `LD_LIBRARY_PATH`, `CMAKE_PREFIX_PATH`,
`PKG_CONFIG_PATH` and — for art/LArSoft — `CET_PLUGIN_PATH`, `FHICL_FILE_PATH` and
`FW_SEARCH_PATH`. The plugin paths matter as much as the library path: a framework
`dlopen`s plugins by name, so that search order is how shadowing reaches code that
was never linked against your build at all.

```console
$ spack splice env --summary
dev packages in front (2): cetlib, cetlib-except
  PATH               <dev>/install/cetlib-vdz32zk/bin   <- cetlib
  LD_LIBRARY_PATH    <dev>/install/cetlib-vdz32zk/lib   <- cetlib
  CET_PLUGIN_PATH    <dev>/install/cetlib-vdz32zk/lib   <- cetlib
```

`--sh`/`--csh`/`--fish` pick the shell; `--json` dumps the resulting environment.

### Verified end to end, twice over

A program linked against the *installed* `cetlib-except`, with a marker edited into
the dev source:

```console
$ ./app                                  # plain shell
---- M5 BEGIN
$ eval "$(spack splice env)"; ./app      # dev build shadows it
==DEV-EXCEPT== M5 BEGIN
```

And dev-on-dev linking, developing `cetlib` and `cetlib-except` together — dev
`cetlib`'s RUNPATH points at the dev `cetlib-except`, not the store:

```console
$ ldd <dev>/install/cetlib-vdz32zk/lib/libcetlib.so | grep cetlib_except
libcetlib_except.so => <dev>/install/cetlib-except-qzr2hzy/lib/libcetlib_except.so
```

### The database record is lossy; the prefix is authoritative

Splice reads the base spec from `<prefix>/.spack/spec.json`, not from the database.
The two carry the same dag hash but not the same graph. Observed here on a package
that was unquestionably built from source:

| source | dependencies |
|---|---|
| database | cetmodules, cmake, compiler-wrapper, gcc-runtime |
| prefix `spec.json` | + **catch2, gcc@12.5.0, glibc, ninja** |

`spack find` files such a package under *"no compilers"*, and reading the DB record
would leave splice with no compiler to inherit and nothing it could rebuild. The
prefix copy is a static file and keeps the full graph.

### Build dependencies that `spack gc` removed

The other half of the same story: a spec can record its build dependencies while
those packages are no longer installed, because `spack gc` prunes build-only deps
once nothing links against them. `splice build` reports that up front instead of
letting cmake fail with `CMAKE_CXX_COMPILER not set`:

```
==> Error: cetlib-except needs 3 build dependencies that are not installed:
      catch2@3.8.0/wzuvryh
      gcc@12.5.0/jwtfpk6
      ninja@1.13.0/ccbnoi4
    They were most likely removed by 'spack gc' ...
```

Recovering does **not** require relaxing the no-concretize rule: the exact concrete
specs of the missing tools are still recorded in the spec, so they can be
reinstalled verbatim.

### An external is a per-host claim, and `installed` never doubts it

The check above skips externals, because an external has no store prefix to be
installed into — `spec.installed` is True for one no matter what is on disk. That
makes externals the one class of dependency nothing verifies, and the claim they
make is **per host**. One store shared between a debian12 machine and an el9
container carries a single record:

```yaml
  ninja:
    externals:
    - spec: ninja @=1.10.2 os=almalinux9
      prefix: /usr
```

true on the machine it was written for and false in the container, whose image
ships no `/usr/bin/ninja`. Spack trusts it, puts `/usr/bin` on PATH, and dies in
`which(..., required=True)` with a message that names no record at all:

```
==> Error: spack requires 'ninja'. Make sure it is in your path.
```

So `check_buildable` probes build-only externals for the executables their own
recipe declares (`ninja` declares `^ninja$`, which is how Spack detects it in the
first place — no second list to keep in sync):

```
==> Error: geant4reweight needs 1 external build tools that are not on this host:
      ninja@1.10.2/y4ukg7l -> /usr
    The external is declared in packages.yaml but its executable is not under that
    prefix here ...
```

Only the **prefix** is probed, and PATH deliberately does not count. The obvious
objection — "surely a working ninja earlier on PATH is good enough" — is wrong, and
the first real build is what proved it:

```python
# spack_repo/builtin/packages/ninja/package.py:117, in setup_dependent_package
which_string(name, path=[self.spec.prefix.bin], required=True)
```

The recipe pins the lookup to its own `prefix.bin`. A ninja on PATH is never
consulted, so a check that accepted one would wave through a build that dies in
`setup_dependent_package` regardless. What has to become true is the record: on
this container, bind-mounting a working ninja onto `/usr/bin/ninja` fixes it
without touching a single hash, where editing the external out of `packages.yaml`
would reconcretize the whole stack.

Three more things keep it from crying wolf. Only **pure build edges** are probed —
an external reached by a link edge is a library, and its executable being absent
says nothing about it. A package that declares no `executables` is left alone. And
a module-provided external is never probed, since there is no prefix to look in.
Verified both ways on the same spec: the check fires in the el9 container and stays
quiet on debian12, where `/usr/bin/ninja` is real.

## Editing the recipe, not just the source

A dev spec is copied from the installed node, and that node records `package.py` as
it was **at install time**. Edit the recipe and the copy is stale — a new `variant`
is missing, so `cmake_args` doing `spec.variants["newvar"]` raises `KeyError`; a new
`depends_on` never reaches `CMAKE_PREFIX_PATH`. Splice reconciles the copy against
the recipe on disk before the hashes settle, and reports what moved:

```console
$ spack splice status
frontier : 9 direct deps -> 12 in the dev_view

recipe drift (1 package(s) differ from their install):
  cetlib-except: +variants drifttest=False; +deps zlib(base spec); -deps catch2
```

Reconciliation is pure lookup — nothing is solved. Variant defaults come from the
recipe; a new dependency is resolved **by name** against the base DAG first, then by
an ABI-filtered store query. If neither has it, splice stops rather than concretizing:

```console
==> Error: cetlib-except now depends on 'cowsay', which is in neither the base spec
    nor the store (for this architecture and runtime)
    Splice will not concretize to invent it. Install it, or add it to the base
    environment and re-run 'spack splice init'.
```

Two things are deliberately shielded from removal. **Compiler and runtime nodes**
(`gcc`, `gcc-runtime`, `glibc`, `compiler-wrapper`) are injected by concretization and
never named in a `depends_on`, so treating their absence as a deletion would strip the
compiler out of every dev spec. **Spack-managed variants** (`dev_path`, `patches`,
`commit`) are not recipe declarations either — dropping `dev_path` would un-develop
the package.

The frontier and `dev_view` are computed from the *reconciled* specs, not the base
DAG, so a newly added dependency shows up in the view. Above, adding `zlib` and
dropping `catch2` moved the frontier 8 → 9 → 8.

**One consequence worth knowing:** a recipe edit changes the spec, so it changes the
hash and therefore the dev prefix — the next `splice build` is a fresh build rather
than an incremental one. Editing *source* leaves the hash alone, which is why that
loop stays at 0.81s. Old prefixes are not currently cleaned up.

## Adding a package the stack never had

```console
$ spack splice add --new cowsay
$ spack splice status
dev set (2 packages to rebuild):
  * cetlib-except@1.09.01
  + cowsay
  (* = chosen by you, + = new, the rest are implied)
```

There is no installed node to copy, so the spec is built from the recipe: version
from the recipe's own preference, variants from its defaults, dependencies resolved
by name against the base DAG (then an ABI-filtered store query, then failure). The
compiler is **borrowed from the base stack** rather than solved for — recipes ask for
one only through the `c`/`cxx` virtuals, which cannot be resolved without the
concretizer, and reusing the stack's compiler is exactly what ABI compatibility
demands anyway.

A new package is an *addition*, not a substitution, so it does not appear in the
woven DAG from `splice spec` — `Spec.splice` can only swap a node that is already
there. It is built last, and if it depends on something you are also developing it
picks up the **dev** build, not the installed one.

## Checking against the recipe's constraints

Nothing verifies that inherited versions and variants still satisfy what the recipe
asks — they were solved for once, and stop matching when you bump a version or edit a
`depends_on`. `splice status` runs a shallow check:

```
==> Warning: 1 recipe constraint(s) not satisfied by the graph:
    cetlib-except: requires 'cetmodules@99.0.0:' but the graph has cetmodules@3.27.03…
```

Shallow on purpose. It asks whether each dependency satisfies the constraint written
next to it, and evaluates `conflicts`. It cannot choose between virtual providers,
propagate a variant across the DAG, or resolve a conflict by picking other versions —
all of which need the solver.

## Moving a dev area to another machine

```console
$ spack splice pack -o mydev.tar.gz
==> packed 1 dev package(s): cetlib-except
  mydev.tar.gz  (0.1 MiB)
  sha256 b4b9de4c…
==> on the target: tar xzf <file> && cd <dir> && . ./setup.sh
```

```
mydev/
├── install/<pkg>-<hash>/…   the built prefixes
├── setup.sh                 source this
├── run.sh                   run.sh <cmd> … — sets the env and execs
├── modulefile               TCL, prepend-path
└── splice.json              provenance: base spec, arch, store root, spliced DAG
```

Only the dev builds travel, so the target must reach the **same Spack store at the
same path** — everything the dev builds depend on still lives there. `setup.sh`
checks and refuses to continue if it's missing. That constraint is what keeps the
artifact at megabytes instead of gigabytes.

Relocation is by `LD_LIBRARY_PATH`, never by rewriting binaries. Unpack anywhere:

```console
$ tar xzf mydev.tar.gz && cd mydev && . ./setup.sh
$ ldd ./app | grep cetlib_except
libcetlib_except.so => …/mydev/install/cetlib-except-zn2pqr7/lib/libcetlib_except.so
```

That works only because dev builds carry DT_RUNPATH. Rewriting them would be actively
dangerous: `relocate.relocate_elf_binaries` falls back to
`patchelf --force-rpath --set-rpath` (`relocate.py:186`) whenever its in-place ELF
patch won't fit, and `--force-rpath` converts DT_RUNPATH to DT_RPATH — silently
destroying the shadowing the whole tool depends on.

### Three traps in generating a portable script

All three were hit and fixed; each fails quietly rather than loudly.

* **Baking in the packing machine.** `EnvironmentModifications.shell_modifications`
  defaults to `env=os.environ` and emits fully *resolved* values, so the artifact
  would carry whatever `PATH` the packing host had. Resolve against `{}` instead.
* **Clobbering the target's `PATH`.** Having fixed the above, a flat
  `export PATH=…` then *replaces* the target's PATH — the unpacked shell had no
  `/usr/bin` and `head: command not found`. Search paths are emitted as
  `"…${PATH:+:$PATH}"`, distinguished from single-valued variables by checking for
  `NamePathModifier`. The modulefile has the same split: `prepend-path` vs `setenv`.
* **`${BASH_SOURCE[0]}` is a bash array reference** and dash rejects it outright with
  `Bad substitution`, killing `run.sh`. Bare `${BASH_SOURCE:-$0}` works everywhere.
  `run.sh` also resolves and exports `SPLICE_ROOT` itself before sourcing, because
  under dash a sourced script's `$0` is the shell, not the file.

Tarballs are built with `spack.util.archive` (zeroed mtimes, normalized ownership and
permissions, sorted entries), so they're reproducible.

## Architecture guard

Spack's install layout has no OS component — everything lands in
`opt/spack/linux-<target>/` — so one store can hold packages built in several
different containers with nothing in the path to tell them apart. Building against a
base spec from a different OS produces binaries linked against the wrong libc, which
is a confusing way to find out.

`splice init` and `splice status` compare the base spec's OS against the running host
and say so:

```
==> Warning: base spec was built for debian12, but this host is almalinux9. Dev
    builds made here will not be ABI-compatible with it, and the installed binaries
    most likely will not run either.
```

## Commands

| Command | Purpose |
|---|---|
| `splice init [<spec\|env\|dir\|file>] [--root S]` | Bind a dev area to a base concrete spec (default: active env) |
| `splice add <pkg>... [--new]` | Pick packages to develop; `--new` for ones not in the spec |
| `splice rm <pkg>...` | Unpick |
| `splice status` | Dev set, frontier, un-shadowable dependents |
| `splice view [--strict]` | (Re)build the dev_view symlink farm |
| `splice spec [--json]` | Weave the dev builds into the base spec, cache the result |
| `splice src [pkg...]` | Fetch sources for dev packages that lack them |
| `splice build [pkg...]` | Compile the dev set into the dev area, with RUNPATH |
| `splice env [--summary]` | Emit the runtime environment; `eval` it |
| `splice pack [-o OUT]` | Tar the dev builds with a generated setup script |
| `splice graph [--dot]` | Render the dev subgraph |

State lives in `<dev-area>/splice.yaml`. Commands find it by walking up from the cwd
(like git), or via `-d/--dir` or `$SPACK_SPLICE_DIR`.

## Tests

```console
$ for t in graph view base specbuild build runtime pack drift check; do spack python spack-splice/tests/test_$t.py; done
```

They use fake DAGs, so they need neither a store nor pytest. (`spack unit-test
--extension=splice` is the intended route, but on this machine Spack's pytest
bootstrap fails for unrelated reasons — missing `Python.h`.)

## Status

M1 (graph analysis), M2 (dev_view), M3 (spec weaving) and M4 (building) are
implemented. Requires Spack ≥ 1.2 (compilers-as-nodes, `spack_repo` layout).

M1–M5 are done and verified against the real 216-node `art-suite` stack: graph
analysis, dev_view, spec weaving, building, and the runtime environment. A
multi-package dev set builds bottom-up with dev-on-dev linking, and the result
shadows the installed stack. The edit→rebuild loop is 0.81s.

Builds have to run on a host matching the stack's OS. The el9 container cannot build
the debian12 stack (its tools need glibc 2.36), and its own el9 stack has had every
build tool removed by `spack gc`. `splice init` warns about the first case and
`splice build` about the second.

M8 (`splice pack`) is done too: dev builds tar up with a generated `setup.sh`,
`run.sh` and modulefile, and relocate correctly to any path.

M6 (recipe drift) is done: variants and dependencies are reconciled against the
recipe on disk, with compiler nodes and Spack-managed variants shielded, and an
unresolvable new dependency refused rather than concretized.

M7 is done as well: `--new` packages built from their recipes, and a shallow
constraint check surfaced in `splice status`.

That completes M1-M8. The one deferred piece is emitting a conda package from
`splice pack --conda`.

M2 was validated by compiling and linking a program against the view: headers and
`libcetlib_except.so` both resolved through `view/`, and the binary came out with
**DT_RUNPATH**, which is what makes `LD_LIBRARY_PATH` shadowing work.

## A trap worth knowing about

`traverse.traverse_nodes([node], direction="parents")` walks parent pointers on Spec
objects **shared across every DAG in the store database**, so it leads out of the DAG you
asked about. Walking parents from `cetlib` in `art-suite` reaches 25 nodes; only 8 are
actually part of `art-suite`. Everything in `graph.py` builds its own adjacency maps
confined to nodes reachable from the given root.
