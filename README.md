# Panda

A terminal quiz game for kids. Reads question/answer files from a git
repository, quizzes the player with a timed multiple-choice test, then stores
per-player per-test session logs back into the same repository and commits +
pushes them.

![urwid TUI](https://img.shields.io/badge/TUI-urwid-blue)
![no external runtime deps](https://img.shields.io/badge/runtime-stdlib%20%2B%20urwid-green)

## Features

- Timed questions, soft timeout (continue after time runs out, recorded as
  "time" failure).
- Six answers chosen with keys **S D F J K L** (case-insensitive).
- Integrity-checked test files (SHA256 sidecars refuse to load tampered
  tests).
- Results logged per-player per-test, committed and pushed to the repo via
  SSH.
- Most-failed questions shown first (random tiebreak).
- Recent tests surfaced at top of the picker.
- Gamified finish messages (perfect / few wrong / mostly timeouts / hard).
- Pure Python (stdlib + urwid). Git and SSH handled via `subprocess`.
- Works without installation: nix-shell shebang pulls `python312`, `urwid`,
  `git`, `openssh`.

## Quick start

### Inside the flake dev shell (recommended for development)

```sh
nix develop
python -m pytest -q        # run tests
./panda.py                  # play
```

### Standalone (any machine with Nix)

Just make the file executable and run it — the nix-shell shebang pulls its
own dependencies:

```sh
chmod +x panda.py
./panda.py
```

### With pip (no Nix)

If you don't use Nix, you can install the Python dependencies with pip.
Panda needs Python 3.11+ (for the stdlib `tomllib` module) plus `urwid`.
Git and SSH must be installed on your system (Panda shells out to them).

#### Runtime only (to play)

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python ./panda.py tests
```

`requirements.txt` contains the single runtime dependency:

```
urwid
```

System packages you also need on PATH:

- `git` (Panda calls it via `subprocess` for clone/pull/commit/push)
- `ssh` (Panda pushes over SSH; your SSH key/agent handles auth)

On Debian/Ubuntu: `sudo apt install git openssh-client python3-venv`
On Fedora: `sudo dnf install git openssh python3-virtualenv`
On macOS: `brew install git openssh` (Python 3.11+ ships with the system
on recent macOS; otherwise `brew install python@3.12`).

#### Development (to run the tests)

The dev requirements include everything from `requirements.txt` plus
`pytest`:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

`requirements-dev.txt`:

```
-r requirements.txt
pytest
```

#### Why the nix-shell shebang if pip works?

The shebang means a kid (or parent) can drop `panda.py` on any Nix machine
and just run it — no venv, no `pip install`, no PATH tweaks. The pip path is
for systems without Nix, or for development inside your editor/IDE. Both
paths use the same `urwid` package; only the installation method differs.

### First run

On first run, Panda asks for a git repo URL (SSH recommended, e.g.
`git@github.com:you/panda-repo.git`). It clones the repo to
`~/.local/share/panda/repo` ($XDG_DATA_HOME/panda/repo) and saves the URL to
`~/.config/panda/config.toml` so you never have to enter it again.

You can also pre-seed the config:

```sh
./panda.py --init git@github.com:you/panda-repo.git
```

### Pre-flight safety check

Before showing the test picker, Panda verifies the local repo is on the
`main` branch, has a clean working tree, and has no unpushed commits. If any
of these fail, Panda refuses to start and tells you what to fix. This keeps
the result history trustworthy.

## Repository layout

A single git repo holds both questions and results. (The game itself does not
live in this repo — put it next to your other dotfiles or scripts.)

```
panda-repo/
├── README.md
├── tests/
│   ├── math-easy.toml             # one test per file
│   ├── math-easy.toml.sha256      # SHA256 of the .toml (git-tracked)
│   ├── geography-eu.toml
│   └── geography-eu.toml.sha256
└── results/
    └── <player>/                  # dir per player (OS user by default)
        └── <test-slug>/           # e.g. math-easy
            └── 2026-08-05T13-00-00Z.toml   # one log per session
```

A starter set of tests lives in `tests/` — copy it, `git init`, push, and
point Panda at it with `panda.py tests`.

## Test file format (`tests/<name>.toml`)

```toml
title = "Math — Easy"          # shown in the picker
timeout = 10                   # default per-question timeout (seconds)

[[questions]]
id = "q1"                      # stable id used for failure stats
question = "What is 7 × 8?"
correct = "56"
answers = ["54","56","64","48","63","42"]
# `answers` is optional. If absent, Panda samples 6 distinct
# answers from the pool of all `correct` values across the file
# (the current question's correct value is always included).

[[questions]]
id = "q2"
question = "What is 15 + 9?"
correct = "24"
timeout = 5                    # per-question override
```

Rules enforced at load time:
- At least one question.
- Each question has a stable `id`, `question`, and `correct`.
- `id`s are unique within the file.
- If `answers` is present: exactly 6 entries, all distinct, and `correct`
  must appear in the list.
- The `.toml.sha256` sidecar must match the file's actual bytes.

### Updating the SHA256 sidecars

Every `tests/*.toml` is paired with a `<name>.toml.sha256` sidecar containing
the SHA256 of the `.toml`'s exact bytes. On load, Panda recomputes the hash
and refuses to start if the sidecar is missing or mismatched — this detects
in-place edits (e.g. by a sneaky kid) and relies on git history for tamper
evidence. **Any change to a `.toml` must be followed by a sidecar refresh,
and both files must be committed together.** A stale sidecar makes the test
unloadable.

#### After editing an existing test

```sh
cd ~/.local/share/panda/repo          # or wherever your local clone lives
$EDITOR tests/multiply.toml           # add/edit questions, save
./panda.py tests --verify             # rehash ALL .toml sidecars
```

`--verify` walks every `tests/*.toml` and overwrites its `.sha256` with
the current content hash. It prints one line per file processed:

```
multiply.toml  ->  multiply.toml.sha256
1 test file(s) verified/sidecars refreshed.
```

Then commit both files in the same commit:

```sh
git add tests/multiply.toml tests/multiply.toml.sha256
git commit -m "tests: add a new multiplication question"
git push
```

#### After adding a new test

Create the `.toml` (no sidecar yet), then run `--verify` — it will create
the missing `.sha256` for you:

```sh
$EDITOR tests/capitals-eu.toml           # author the file (no .sha256 yet)
./panda.py tests --verify
git add tests/capitals-eu.toml tests/capitals-eu.toml.sha256
git commit -m "tests: add European capitals"
git push
```

#### Verifying without writing

There is no separate read-only verify subcommand; the game itself performs
the check on every load and refuses to start if the sidecar is wrong. If you
want to probe a single file without rehashing everything, use Python directly:

```sh
python3 -c "
import sys, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('p', '/home/sirex/dev/panda/panda.py')
m = importlib.util.module_from_spec(spec); sys.modules['p'] = m; spec.loader.exec_module(m)
p = 'tests/multiply.toml'
print('OK' if m.verify_sha256_sidecar(Path(p)) else 'TAMPERED', p)
"
```

#### What if Panda refuses to load my test?

If you see `integrity check failed ... sha256 sidecar missing or mismatched`,
it means the `.toml` no longer matches its `.sha256`. Either the file was
hand-edited or the sidecar was not refreshed after an edit. Re-run
`--verify`, then commit the pair.

#### Why a sidecar instead of GPG signing?

For a kids' quiz the sidecar is enough: it catches accidental or cheeky edits
locally, and git history makes any committed change attributable. GPG signing
is stronger but adds key-management overhead that's overkill here. The sidecar
is also binary-free and easy to inspect:

```sh
cat tests/multiply.toml.sha256          # 64 hex chars + newline
sha256sum tests/multiply.toml | cut -d' ' -f1   # compare manually
```

## Result log format

One file per session, at
`results/<player>/<test-slug>/<ISO-timestamp>.toml`:

```toml
test = "math-easy"
test_title = "Math — Easy"
player = "alex"
commit = "abc123def..."                 # HEAD commit id of repo at session start
started_at = 2026-08-05T13:00:00Z
duration_seconds = 120.0
total_questions = 10
correct = 7
wrong = 2
timed_out = 1

# Only INCORRECT answers are logged:
[[mistakes]]
question = "q2"
answer = "25"
took = 5.0
reason = "wrong"          # or "time" (failed on timeout)
```

## Config (`~/.config/panda/config.toml`)

```toml
player = "alex"
repo_url = "git@github.com:you/panda-repo.git"
local_repo = "~/.local/share/panda/repo"
```

`player` defaults to `$USER`. `local_repo` defaults to
`$XDG_DATA_HOME/panda/repo`.

## Game flow

1. Load config; if no repo_url, prompt once and persist.
2. Clone or `git pull --ff-only` the local repo.
3. Pre-flight check: clean `main` with no unpushed commits.
4. Filter-typed picker lists tests with `title — attempts: N, last: A/B`.
   Recent tests starred at top. ↑/↓ + Enter to pick; q to quit.
5. Confirm screen: Enter to start, Esc to go back.
6. Order questions: most-failed first (random tiebreak).
7. Each question shows the question, a 3×2 grid of answer keys (S D F / J K L)
   centered, a progress bar for time remaining (numeric seconds shown only
   when timeout > 5), and a stats line: `Left: N   ✓ on-time: A   ✗ wrong:
   B   ✳ time: C`.
8. Press S/D/F/J/K/L (case-insensitive). Wrong key ignored. If time runs
   out, the indicator flips to `TIME UP` but the player must still press an
   answer key to advance (the answer is recorded as failed-on-time).
9. Esc during test → modal "Stop current test? [Y/N]"; q → modal "Quit?".
10. When finished, write the result log and `git add results && git commit
    && git push` (author `Panda <panda@localhost>`).
11. Show a gamified summary, then loop back to the picker.

## .desktop launcher

Install `panda.desktop` to `~/.local/share/applications/`:

```sh
mkdir -p ~/.local/share/applications
cp panda.desktop ~/.local/share/applications/
# If you installed panda.py elsewhere, edit the Exec= line accordingly.
```

`Terminal=true` makes the desktop environment spawn a terminal emulator and
run the script inside it. The script's nix-shell shebang pulls its own
dependencies, so no extra setup is needed on the host.

## Tests

```sh
nix develop
python -m pytest -q
```

Or without entering the shell:

```sh
nix run .#tests            # via the flake app
```


## starter tests

Copy `tests/` irectory into a fresh git repo and push it to make a Panda
questions/results repository.

```sh
mkdir -p ~/.local/share/panda/repo
cp -r tests ~/.local/share/panda/repo/tests
cd ~/.local/share/panda/repo
git init -b main
git add .
git commit -m "Initial"
git remote add origin git@github.com:you/panda-repo.git
git push -u origin main
```

Then point the game at it:

```sh
./panda.py --init git@github.com:you/panda-repo.git
```

## Layout

```
.
└── tests/
    ├── multiply.toml
    └── multiply.toml.sha256
```

- Add one `.toml` per test in `tests/`.
- Run `./panda.py --verify` after editing tests to refresh the
  `.sha256` sidecars, then commit both the `.toml` and its `.sha256`.
