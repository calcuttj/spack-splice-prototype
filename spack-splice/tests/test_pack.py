"""Tests for the generated setup script, modulefile and quoting.

Building an actual tarball needs built prefixes, so that is covered by the M8
acceptance run. What is unit-testable here is the script generation, which is where
the portability traps live: clobbering the target's PATH, baking in the packing
machine's environment, and quoting that stops ``${SPLICE_ROOT}`` expanding.
"""

import os

try:
    from spack.extensions.splice import pack
except ImportError:
    # Running standalone, without the extension registered in config:extensions.
    import spack.extensions

    spack.extensions.ensure_extension_loaded(
        "splice", path=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from spack.extensions.splice import pack

DEV = "/dev/area"
STORE = "/opt/store"


def test_search_paths_extend_rather_than_replace():
    """The bug this guards: a flat assignment leaves the target with no /usr/bin.

    Observed for real -- 'head: command not found' after sourcing a script that
    set PATH outright.
    """
    (line,) = pack._export_lines({"PATH": "/opt/store/x/bin"}, {"PATH"}, DEV)
    assert line == 'export PATH="/opt/store/x/bin${PATH:+:$PATH}";'


def test_single_valued_variables_are_just_set():
    (line,) = pack._export_lines({"ROOTSYS": "/opt/store/root"}, set(), DEV)
    assert line == "export ROOTSYS=/opt/store/root;"


def test_dev_paths_become_relocatable():
    (line,) = pack._export_lines({"LD_LIBRARY_PATH": f"{DEV}/install/p/lib"}, {"LD_LIBRARY_PATH"}, DEV)
    assert "${SPLICE_ROOT}/install/p/lib" in line
    assert DEV not in line


def test_placeholder_is_double_quoted_so_it_expands():
    """shlex.quote would wrap it in single quotes and it would never expand."""
    (line,) = pack._export_lines({"FHICL_FILE_PATH": f"{DEV}/install/p/fcl"}, set(), DEV)
    assert line.startswith('export FHICL_FILE_PATH="')
    assert "'" not in line


def test_shell_metacharacters_are_escaped():
    """Nothing inside the double quotes may still be live to the shell.

    Checking for absence of the raw text is not enough -- an escaped '\\$HOME' still
    contains the substring '$HOME'. What matters is that every special character is
    backslash-escaped, apart from the ${SPLICE_ROOT} we deliberately want expanded.
    """
    import re

    value = f'{DEV}/x`whoami`$HOME"q"'
    (line,) = pack._export_lines({"V": value}, set(), DEV)

    body = line[len('export V="') : -len('";')]
    assert "${SPLICE_ROOT}" in body
    rest = body.replace("${SPLICE_ROOT}", "")
    unescaped = re.search(r'(?<!\\)[`"$]', rest)
    assert unescaped is None, f"unescaped {unescaped.group()!r} in {rest!r}"


def test_dev_area_state_var_is_not_exported():
    """SPACK_SPLICE_DIR names the packing machine's dev area; meaningless elsewhere."""
    assert pack._export_lines({"SPACK_SPLICE_DIR": DEV}, set(), DEV) == []


def test_modulefile_prepends_search_paths():
    text = pack.modulefile({"PATH": "/opt/store/a/bin"}, {"PATH"}, DEV, STORE)
    assert "prepend-path PATH {/opt/store/a/bin}" in text
    assert "setenv PATH" not in text


def test_modulefile_setenvs_plain_values():
    text = pack.modulefile({"ROOTSYS": "/opt/store/root"}, set(), DEV, STORE)
    assert "setenv ROOTSYS {/opt/store/root}" in text


def test_modulefile_makes_dev_paths_relative_to_the_module():
    text = pack.modulefile({"LD_LIBRARY_PATH": f"{DEV}/install/p/lib"}, {"LD_LIBRARY_PATH"}, DEV, STORE)
    assert 'prepend-path LD_LIBRARY_PATH [file join $root "install/p/lib"]' in text
    assert DEV not in text


def test_modulefile_prepend_order_is_reversed():
    """Each prepend pushes to the front, so emitting in reverse restores the order."""
    text = pack.modulefile({"PATH": "/a:/b:/c"}, {"PATH"}, DEV, STORE)
    order = [ln.split()[-1] for ln in text.splitlines() if ln.startswith("prepend-path PATH")]
    assert order == ["{/c}", "{/b}", "{/a}"]


def test_setup_script_refuses_a_missing_store():
    text = pack.setup_script({}, set(), DEV, STORE)
    assert STORE in text and "not found" in text


def test_root_discovery_avoids_bash_array_syntax():
    """${BASH_SOURCE[0]} makes dash abort with 'Bad substitution'."""
    assert "BASH_SOURCE[0]" not in pack.ROOT_DISCOVERY
    assert "${BASH_SOURCE:-$0}" in pack.ROOT_DISCOVERY


def test_run_script_exports_the_root_before_sourcing():
    """Under dash a sourced script's $0 is the shell, so run.sh must resolve it."""
    text = pack.run_script()
    assert f"export {pack.ROOT_VAR}" in text
    assert text.index(f"export {pack.ROOT_VAR}") < text.index("setup.sh")
    assert 'exec "$@"' in text


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
