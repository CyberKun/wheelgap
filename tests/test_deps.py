"""--deps (#353): package names on the command line, with what they install resolved per target from PyPI's
metadata. A fake JSON API serves each project page and each release page with its Requires-Dist."""

import json
import urllib.error

import pytest
from packaging.requirements import Requirement

import wheelgap
from wheelgap import deps, pypi
from wheelgap.cli import main
from wheelgap.deps import DepsError, Resolver

ANY = "py3-none-any.whl"
LINUX = "cp314-cp314-manylinux_2_17_x86_64.whl"
WIN_AMD = "cp314-cp314-win_amd64.whl"
WIN_ARM = "cp314-cp314-win_arm64.whl"


def release(requires=(), files=(ANY,), requires_python=None, yanked=False, sdist=True):
    return {"requires": list(requires) if requires is not None else None, "files": list(files),
            "requires_python": requires_python, "yanked": yanked, "sdist": sdist}  # fmt: skip


# name -> version -> release. "app" needs lib[crypto]; lib's crypto extra needs crypt, which has no Windows ARM
# wheel; on Windows lib also needs winonly.
INDEX = {
    "app": {"1.0": release(["lib[crypto]>=1"])},
    "plain": {"1.0": release(["lib>=1"])},
    "lib": {
        "1.0": release(),
        "2.0": release(["crypt>=1; extra == 'crypto'", "winonly; sys_platform == 'win32'"]),
        "3.0": release(requires_python=">=3.15"),  # newest, but not for 3.14
    },
    "crypt": {"1.0": release(files=(LINUX, WIN_AMD))},
    "winonly": {"1.0": release(files=(WIN_ARM, WIN_AMD))},
    # a and b disagree about c: the newest c that suits both is 1.5.
    "a": {"1.0": release(["c>=1"])},
    "b": {"1.0": release(["c<2"])},
    "c": {"1.0": release(), "1.5": release(), "2.0": release()},
    "x": {"1.0": release(["c>=2"])},
    "y": {"1.0": release(["c<2"])},
    "yank": {"1.0": release(), "2.0": release(yanked=True)},
    "nometa": {"1.0": release(requires=None, files=())},  # sdist only and no Requires-Dist: can't tell
}


@pytest.fixture
def index(monkeypatch):
    calls = []

    def entries(name, version, r):
        out = [{"filename": f"{name}-{version}-{tag}", "requires_python": r["requires_python"],
                "yanked": r["yanked"]} for tag in r["files"]]  # fmt: skip
        if r["sdist"]:
            out.append({"filename": f"{name}-{version}.tar.gz", "requires_python": r["requires_python"],
                        "yanked": r["yanked"]})  # fmt: skip
        return out

    def fetch_json(url, timeout=20.0, attempts=4, max_bytes=None, deadline=None):
        calls.append(url)
        parts = url.removeprefix(pypi.index_url() + "/").removesuffix("/json").split("/")
        name = parts[0]
        if name not in INDEX:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        versions = INDEX[name]
        if len(parts) == 1:
            latest = next(reversed(versions))
            return {"info": {"name": name, "version": latest},
                    "releases": {v: entries(name, v, r) for v, r in versions.items()},
                    "urls": entries(name, latest, versions[latest])}  # fmt: skip
        r = versions.get(parts[1])
        if r is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        return {"info": {"name": name, "version": parts[1], "requires_dist": r["requires"]},
                "urls": entries(name, parts[1], r)}  # fmt: skip

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    return calls


WIN, LIN = wheelgap.make_targets(["3.14"], ["win-arm64", "linux-x86_64"])


def resolve(*specs, target=WIN):
    tree = Resolver().resolve([Requirement(s) for s in specs], target)
    return tree.chosen, tree.errors, tree.paths


def test_extras_and_markers_are_followed_per_target(index):
    chosen, errors, paths = resolve("app==1.0")
    assert (chosen, errors) == ({"app": "1.0", "lib": "2.0", "crypt": "1.0", "winonly": "1.0"}, {})
    assert paths["crypt"] == ["app", "lib", "crypt"]
    assert resolve("app==1.0", target=LIN)[0] == {"app": "1.0", "lib": "2.0", "crypt": "1.0"}  # no winonly
    assert resolve("plain")[0] == {"plain": "1.0", "lib": "2.0", "winonly": "1.0"}  # lib without [crypto]: no crypt


def test_requires_python_and_yanked_releases_are_skipped(index):
    assert resolve("lib")[0]["lib"] == "2.0"  # 3.0 needs Python 3.15
    assert resolve("lib", target=wheelgap.make_targets(["3.15"], ["win-arm64"])[0])[0]["lib"] == "3.0"
    assert resolve("yank")[0] == {"yank": "1.0"}
    assert resolve("yank==2.0")[0] == {"yank": "2.0"}  # pip installs a yanked release pinned with ==


def test_a_later_constraint_restarts_with_it_known(index):
    assert resolve("a", "b")[0] == {"a": "1.0", "b": "1.0", "c": "1.5"}  # not c 2.0, which b rules out


def test_constraints_that_cant_all_be_met_refuse(index):
    with pytest.raises(DepsError, match="can't resolve c for Python 3.14 on win-arm64: no release meets <2, >=2"):
        resolve("x", "y")


def test_a_release_whose_dependencies_cant_be_read_is_an_error_not_a_clean_bill(index):
    chosen, errors, _ = resolve("nometa")
    assert chosen == {"nometa": "1.0"}
    assert errors == {"nometa": "couldn't read its dependencies: PyPI lists no dependencies for it and it has no "
                                "wheel to confirm that"}  # fmt: skip
    assert resolve("missing")[1] == {"missing": "not on index"}


def test_index_answers_are_shared_between_targets(index):
    resolver = Resolver()
    resolver.resolve([Requirement("app==1.0")], WIN)
    asked = len(index)
    resolver.resolve([Requirement("app==1.0")], LIN)
    assert len(index) == asked


def test_cli_checks_what_the_names_install(index, capsys):
    code = main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "-t", "linux-x86_64", "--json", "--explain",
                 "--no-cache", "--all"])  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    rows = {p["name"]: p for p in out["packages"]}
    assert sorted(rows) == ["app", "crypt", "lib", "winonly"]
    assert rows["crypt"]["results"] == {"win-arm64": "source", "linux-x86_64": "wheel"}
    assert rows["crypt"]["via"] == {"win-arm64": ["app", "lib", "crypt"]}
    assert rows["winonly"]["results"] == {"win-arm64": "wheel", "linux-x86_64": "n/a"}
    assert code == 1


def test_cli_text_names_the_path_and_notes_how_it_resolved(index, capsys):
    assert main(["app==1.0", "--deps", "-p", "3.14", "-t", "win-arm64", "--explain", "--no-cache"]) == 1
    captured = capsys.readouterr()
    assert "  via: app → lib → crypt\n" in captured.out
    assert "--deps resolved 4 packages from PyPI's metadata" in captured.err


def test_without_deps_only_the_named_package_is_checked(index, capsys):
    assert main(["app==1.0", "-p", "3.14", "-t", "win-arm64", "--json", "--no-cache", "--all"]) == 0
    assert [p["name"] for p in json.loads(capsys.readouterr().out)["packages"]] == ["app"]


def test_python_specific_picks_are_separate_rows(index, capsys):
    assert main(["lib", "--deps", "-p", "3.14", "-p", "3.15", "-t", "linux-x86_64", "--json", "--no-cache",
                 "--all"]) == 0  # fmt: skip
    rows = [(p["version"], p["results"]) for p in json.loads(capsys.readouterr().out)["packages"]]
    assert sorted(rows) == [
        ("2.0", {"3.14 linux-x86_64": "wheel", "3.15 linux-x86_64": "n/a"}),
        ("3.0", {"3.14 linux-x86_64": "n/a", "3.15 linux-x86_64": "wheel"}),
    ]


def test_an_unresolvable_tree_is_an_input_error(index, capsys):
    assert main(["x", "y", "--deps", "-p", "3.14", "-t", "win-arm64", "--no-cache"]) == 2
    assert "wheelgap: error: --deps: can't resolve c" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--offline"], "--offline: --deps would look packages up on PyPI"),
        (["--index-url", "https://example.org/simple"], "--deps reads dependencies from PyPI's JSON API"),
    ],
)
def test_deps_refuses_what_it_cant_do(index, capsys, args, message):
    assert main(["app==1.0", "--deps", *args]) == 2
    assert message in capsys.readouterr().err


def test_deps_needs_package_names(tmp_path, index, capsys):
    (tmp_path / "requirements.txt").write_text("lib==2.0\n", encoding="utf-8")
    assert main([str(tmp_path / "requirements.txt"), "--deps"]) == 2
    assert "--deps applies to package names on the command line" in capsys.readouterr().err


def test_round_limit_refuses(index, monkeypatch):
    monkeypatch.setattr(deps, "MAX_ROUNDS", 1)
    with pytest.raises(DepsError, match="can't settle on releases"):
        resolve("a", "b")
