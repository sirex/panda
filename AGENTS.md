# AGENTS.md

Single-file Python quiz game. The whole app is `panda.py` (no package, no
`__init__.py`); `tests/` here is the **starter question-bank** shipped with
the repo, not the test suite.

## Commands

```sh
nix develop                        # dev shell: python312, urwid, pytest, git, openssh, ruff, mypy
python -m pytest -q                # run the suite (test_panda.py)
python -m pytest test_panda.py::test_slugify_basic -q   # single test
nix run .#tests                    # same, via the flake app
ruff check .                       # linter (selects E/W/F/I/UP/B; see pyproject.toml)
ruff format .                      # formatter (100-col, py312 target)
mypy panda.py                      # type checker
nix run .#lint                     # ruff check + format --check, via flake app
nix run .#typecheck                # mypy panda.py, via flake app
./panda.py                         # play (uses configured local_repo/tests)
./panda.py tests                   # play from a specific tests dir
```

Required Python is 3.11+ (stdlib `tomllib`); the flake pins 3.12. Runtime
deps: `urwid` only (`requirements.txt`). Dev adds `pytest`, `ruff`, `mypy`
(`requirements-dev.txt`). `git` must be on PATH (Panda shells out for
clone/pull/commit/push).

## Verification workflow (after editing panda.py)

Always run, in order: **lint → typecheck → test**. The flake apps run the
first two from a known-good toolchain:

```sh
ruff check . && ruff format --check .   # or: nix run .#lint
mypy panda.py                           # or: nix run .#typecheck
python -m pytest -q                      # or: nix run .#tests
```

`ruff check .` auto-fixes much of what it flags (`ruff check . --fix`) —
re-run `ruff format .` afterward, since fixes can change formatting.

## Type annotation policy

mypy is configured in `pyproject.toml` to **require type annotations**:
`disallow_untyped_defs` and `disallow_incomplete_defs` are on, so every
function (including nested closures) must annotate params and return types;
`disallow_any_generics` forces `dict`/`list`/`CompletedProcess` to carry
type parameters. The existing code already passes — keep it that way.

urwid is untyped, so `[[tool.mypy.overrides]] module = ["urwid", "urwid.*"]`
sets `ignore_missing_imports = true` and urwid widgets are `Any`; this is
why `disallow_subclassing_any` and `warn_return_any` are *off* (otherwise
every `class X(urwid.Y)`/urwid return would fail). Don't turn those on.

`test_panda.py` is exempted from the strict annotation rules (it imports
`panda` via importlib and uses pytest fixtures) — see the `test_panda`
override block in `pyproject.toml`.

## Real CLI (README has stale examples)

`panda.py` uses `argparse` with: positional `tests_dir` (optional) +
`--init URL` + `--verify`. There is **no `init-repo` subcommand** and no
two-positional-arg form despite what README examples show. Trust the code
in `panda.py` (`main`).

## Editing tests in `tests/`

Every `tests/*.toml` has a paired `<name>.toml.sha256` sidecar (SHA256 of
the `.toml`'s exact bytes). On load, Panda recomputes the hash and raises
`IntegrityError` if the sidecar is missing or mismatched — a tampered test
is unloadable. Workflow:

```sh
$EDITOR tests/multiply.toml
./panda.py --verify          # refreshes ALL .sha256 sidecars (writes/creates)
git add tests/multiply.toml tests/multiply.toml.sha256   # commit the pair
```

`--verify` takes an optional `tests_dir` positional; with no arg it uses the
configured one. There is no read-only verify — the game's load-time check
is the verification.

## Test suite gotchas

- `test_panda.py` imports `panda.py` via `importlib.util` because the module
  name `panda` is not a package — keep that pattern if adding test files.
- Several tests shell out to `git init`/`commit` (see `_git_init`); `git`
  on PATH is mandatory even for `pytest`. No network needed — pushes are
  tested against a local repo with no remote.
- `_git_init` sets `user.email`/`user.name` itself, so no global git config
  is required.

## Architecture notes

- Result logs are written to `results/<player>/<test-slug>/<ISO>.toml` and
  `git commit && git push`-ed back to the questions repo over SSH (commit
  author `Panda <panda@localhost>`).
- Pre-flight check refuses to start unless the local repo is on `main`,
  clean, and has no unpushed commits (`panda.py` `git_clean_check`).
- Config: `~/.config/panda/config.toml`; data: `$XDG_DATA_HOME/panda/repo`.
- Question order: most-failed first (random tiebreak). Answer keys are
  `S D F J K L` (case-insensitive), 6 answers per question.