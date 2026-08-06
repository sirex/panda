#!/usr/bin/env nix-shell
#! nix-shell -i python3 -p python312 python312Packages.urwid git openssh
"""Panda — a terminal quiz game for kids.

Reads question/answer files from a git repo, presents a timed quiz, stores
per-player per-test session logs back into the same repo, and commits/pushes.

Stdlib + urwid only. Git is driven via subprocess; SSH key handled by the
user's agent (no --pure in the nix-shell shebang).

Usage:
    panda.py                       # play (uses ~/.local/share/panda/repo/tests)
    panda.py tests                 # play, reading tests from ./tests
    panda.py [<url>] --init        # clone <url> into local_repo and save URL
    panda.py [tests_dir] --verify  # refresh tests/*.sha256 sidecars
"""

import argparse
import datetime as _dt
import hashlib
import os
import random
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import urwid
except ImportError:  # pragma: no cover - exercised only outside nix-shell
    sys.stderr.write(
        "panda: urwid not found. Run inside the flake dev shell or use the\n"
        "nix-shell shebang at the top of this file.\n"
    )
    raise


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Six answer keys; left hand S D F, right hand J K L. Case-insensitive.
ANSWER_KEYS = ("s", "d", "f", "j", "k", "l")
# Unicode block glyphs for progress bars.
BAR_FULL = "\u2500"  # ─ light horizontal (less distracting than █)
BAR_EMPTY = "\u2500"  # ─ same glyph, dimmed via palette

# Gamification message banks. Random pick at end of test.
PERFECT_MSGS = [
    "PERFECT! You answered everything correctly. Panda is impressed.",
    "Flawless victory! Not a single mistake. Have a virtual high-five.",
    "All green! The panda council awards you the title 'Question Whisperer'.",
]
FEW_WRONG_MSGS = [  # 1-2 wrong
    "Great run! Just a tiny stumble. One more round?",
    "So close to perfect. The panda believes in you.",
    "Brilliant work. A handful of misses, but mostly magnificent.",
]
SOME_TIMEOUT_MSGS = [  # 0 wrong but some timeouts
    "Every answer was right — just a little quicker next time.",
    "Accuracy: perfect. Speed: a panda could beat you. Try again!",
    "All correct, but the clock beat you. Blink less, think more.",
]
HARD_MSGS = [  # mostly wrong
    "Tough one. The panda once failed this too. Want a rematch?",
    "That was tricky. Take a breath, look over the questions, try again.",
    "No worries — every expert was once a beginner. One more round?",
]


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

ANSWER_KEYS_LOWER = tuple(k.lower() for k in ANSWER_KEYS)


def normalize_key(key: object) -> str | None:
    """Map any urwid key object to one of ANSWER_KEYS_LOWER or None.

    Returns None for non-answer keys (so callers can pass them on).
    Always case-insensitive.
    """
    if not isinstance(key, str):
        return None
    if key.startswith("ctrl ") or key.startswith("meta "):
        return None
    k = key.lower()
    # 'enter', 'esc', 'q', 'y', 'n' are not answer keys
    if len(k) == 1 and k in ANSWER_KEYS_LOWER:
        return k
    return None


def render_bar(
    fraction: float, width: int = 30, full_attr: str = "bar", empty_attr: str = "bar_dim"
) -> list[tuple[str, str]]:
    """Return urwid Text markup: a thin horizontal line with the `fraction`
    portion in `full_attr` and the remainder in `empty_attr`. Both segments
    use the same light horizontal glyph `─`; differentiation is by colour
    only, so the bar stays unobtrusive."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    markup: list[tuple[str, str]] = []
    if filled:
        markup.append((full_attr, BAR_FULL * filled))
    if width - filled:
        markup.append((empty_attr, BAR_EMPTY * (width - filled)))
    return markup


def iso_now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(s: str) -> str:
    out = []
    for ch in s.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def toml_escape(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{s}"'


def toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return toml_escape(v)
    if isinstance(v, _dt.datetime):
        # TOML offset-date-time: keep UTC 'Z'-style as RFC3339
        return toml_escape(v.strftime("%Y-%m-%dT%H:%M:%SZ"))
    raise TypeError(f"can't serialise {type(v)!r} to TOML")


def write_toml(
    path: Path,
    data: dict[str, object],
    arrays: list[tuple[str, list[dict[str, object]]]] | None = None,
) -> None:
    """Write a flat header dict plus optional [[array]] records to `path`.

    `arrays` is a list of (name, list[dict]) pairs. Each dict has scalar
    string-only values.
    """
    lines: list[str] = []
    for k, v in data.items():
        lines.append(f"{k} = {toml_value(v)}")
    if arrays:
        for name, rows in arrays:
            for row in rows:
                lines.append("")
                lines.append(f"[[{name}]]")
                for k, v in row.items():
                    lines.append(f"{k} = {toml_value(v)}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "panda"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "panda"
DEFAULT_REPO_LOCAL = DATA_DIR / "repo"


@dataclass
class Config:
    player: str = ""
    repo_url: str = ""
    local_repo: Path = field(default_factory=lambda: DEFAULT_REPO_LOCAL)
    repo_disabled: bool = False
    tests_dir: Path = field(default_factory=lambda: DEFAULT_REPO_LOCAL / TESTS_SUBDIR)

    @property
    def effective_player(self) -> str:
        return self.player or os.environ.get("USER", "anonymous")


def tests_dir_for(cfg: Config) -> Path:
    """Where *.toml test files live for this run — the persisted config value
    by default, or a one-shot CLI override."""
    return cfg.tests_dir


def load_config() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
        cfg.player = data.get("player", "")
        cfg.repo_url = data.get("repo_url", "")
        cfg.repo_disabled = bool(data.get("repo_disabled", False))
        lr = data.get("local_repo")
        if lr:
            cfg.local_repo = Path(os.path.expanduser(lr))
        td = data.get("tests_dir")
        if td:
            cfg.tests_dir = Path(os.path.expanduser(td))
    return cfg


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "player": cfg.player or cfg.effective_player,
        "repo_url": cfg.repo_url,
        "local_repo": str(cfg.local_repo),
        "repo_disabled": cfg.repo_disabled,
        "tests_dir": str(cfg.tests_dir),
    }
    write_toml(CONFIG_PATH, data)


# --------------------------------------------------------------------------- #
# Git subprocess wrappers
# --------------------------------------------------------------------------- #


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    # Encourage English output so our parsers stay stable.
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def ensure_repo(cfg: Config) -> None:
    cfg.local_repo = cfg.local_repo.expanduser()
    cfg.local_repo.mkdir(parents=True, exist_ok=True)
    if not (cfg.local_repo / ".git").exists():
        if not cfg.repo_url:
            return  # caller will prompt
        git(cfg.local_repo.parent, "clone", cfg.repo_url, str(cfg.local_repo))
    else:
        # pull latest questions/results
        try:
            git(cfg.local_repo, "pull", "--ff-only")
        except subprocess.CalledProcessError as e:
            # continue anyway; user may resolve manually
            sys.stderr.write(f"panda: git pull failed: {e.stderr.strip()}\n")


def get_commit_id(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def git_state_is_clean(repo: Path) -> tuple[bool, str]:
    """Return (ok, message). ok means: branch == main; working tree clean; no
    unpushed commits (if upstream exists, HEAD == @{u})."""
    try:
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, f"git rev-parse failed: {e.stderr.strip()}"
    if branch != "main":
        # Fallback: master is acceptable (legacy default).
        if branch != "master":
            return False, f"not on main branch (currently '{branch}')."

    status = git(repo, "status", "--porcelain").stdout.strip()
    if status:
        return False, "working tree is not clean:\n" + status

    # Unpushed commits: compare HEAD to @{u}.
    try:
        head_up = git(repo, "rev-parse", "@{u}").stdout.strip()
    except subprocess.CalledProcessError:
        head_up = ""
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if head_up and head != head_up:
        return False, "there are unpushed commits (HEAD != @{u})."
    return True, f"clean on branch '{branch}' at {head[:8]}."


def commit_and_push_results(repo: Path, player: str) -> tuple[bool, str]:
    """Stage results/, commit with a stable Panda identity, push."""
    try:
        git(repo, "add", "results")
    except subprocess.CalledProcessError as e:
        return False, f"git add failed: {e.stderr.strip()}"
    # anything to commit?
    staged = git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if not staged:
        return True, "nothing to commit"
    msg = f"results: {player} {iso_now()}"
    try:
        git(
            repo,
            "-c",
            "user.name=Panda",
            "-c",
            "user.email=panda@localhost",
            "commit",
            "-m",
            msg,
        )
    except subprocess.CalledProcessError as e:
        return False, f"git commit failed: {e.stderr.strip()}"
    try:
        git(repo, "push")
    except subprocess.CalledProcessError as e:
        return False, f"git push failed: {e.stderr.strip()}"
    return True, "committed and pushed"


# --------------------------------------------------------------------------- #
# Test file loading + integrity
# --------------------------------------------------------------------------- #

TESTS_SUBDIR = "tests"
RESULTS_SUBDIR = "results"


@dataclass
class Question:
    id: str
    question: str
    correct: str
    answers: list[str] | None = None
    timeout: int | None = None


@dataclass
class Test:
    slug: str
    title: str
    timeout: int
    questions: list[Question]
    path: Path


# (correct, wrong, timed_out, total) for a single session.
Score = tuple[int, int, int, int]
# (slug, title, attempts, last_score, best_score) as returned by list_tests.
TestInfo = tuple[str, str, int, Score, Score]


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def verify_sha256_sidecar(toml_path: Path) -> bool:
    """Return True if the .sha256 sidecar exists and matches. Missing sidecar
    is treated as a verification failure (test is considered tampered)."""
    side = sha256_sidecar(toml_path)
    if not side.exists():
        return False
    expected = side.read_text().strip().split()[0].lower()
    if len(expected) != 64:
        return False
    actual = sha256_of_file(toml_path)
    return actual.lower() == expected


def write_sha256_sidecar(toml_path: Path) -> Path:
    side = sha256_sidecar(toml_path)
    side.write_text(sha256_of_file(toml_path) + "\n", encoding="utf-8")
    return side


def parse_test(toml_path: Path) -> Test:
    slug = toml_path.stem
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    title = data.get("title", slug)
    timeout = int(data.get("timeout", 10))
    qs_raw = data.get("questions", [])
    if not qs_raw:
        raise ValueError(f"{toml_path}: no questions")
    questions: list[Question] = []
    ids_seen: set[str] = set()
    for i, q in enumerate(qs_raw):
        qid = q.get("id") or f"q{i}"
        if qid in ids_seen:
            raise ValueError(f"{toml_path}: duplicate question id {qid!r}")
        ids_seen.add(qid)
        if "question" not in q or "correct" not in q:
            raise ValueError(f"{toml_path}: question {qid!r} needs 'question' and 'correct'")
        answers = q.get("answers")
        if answers is not None:
            if len(answers) != 6:
                raise ValueError(f"{toml_path}: question {qid!r} must have 6 answers")
            if len(set(answers)) != 6:
                raise ValueError(f"{toml_path}: question {qid!r} has duplicate answers")
            if q["correct"] not in answers:
                raise ValueError(f"{toml_path}: question {qid!r}'s 'correct' not in answers")
        questions.append(
            Question(
                id=qid,
                question=q["question"],
                correct=str(q["correct"]),
                answers=list(answers) if answers is not None else None,
                timeout=int(q["timeout"]) if "timeout" in q else None,
            )
        )
    return Test(slug=slug, title=title, timeout=timeout, questions=questions, path=toml_path)


def load_test_safely(toml_path: Path) -> Test:
    if not verify_sha256_sidecar(toml_path):
        raise IntegrityError(
            f"integrity check failed for {toml_path.name}: "
            "sha256 sidecar missing or mismatched (file may have been tampered with)."
        )
    return parse_test(toml_path)


class IntegrityError(Exception):
    pass


def collect_answer_pool(test: Test) -> list[str]:
    """All `correct` values across the test — used to sample random answers
    when a question has no explicit answers list."""
    pool = [q.correct for q in test.questions]
    # de-duplicate preserving order: we want variety, but random.sample needs
    # unique items; we dedupe so the pick-6 logic can always reach 6 if the
    # pool has at least 6 distinct strings.
    seen = set()
    deduped = []
    for a in pool:
        if a not in seen:
            seen.add(a)
            deduped.append(a)
    return deduped


def sample_six_answers(question: Question, pool: list[str], rng: random.Random) -> list[str]:
    """Return a list of 6 distinct answers containing `question.correct`.

    If the question itself has explicit answers, validate+return those.
    The pool sampling fallback flips 6 random distinct items from the pool,
    ensuring the correct answer is present by ensuring correct+5 distractors.
    """
    if question.answers is not None:
        return list(question.answers)
    out = [question.correct]
    distractors = [a for a in pool if a != question.correct]
    if len(distractors) < 5:
        # Not enough distinct distractors; pad with explicit variants of the
        # distractors we have (with a numeric suffix). Better than erroring.
        i = 0
        seen = {question.correct}
        for d in distractors:
            seen.add(d)
        while len(out) < 6:
            cand = question.correct + " " * i
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
            i += 1
        return out[:6]
    chosen = rng.sample(distractors, 5)
    out.extend(chosen)
    rng.shuffle(out)
    return out


# --------------------------------------------------------------------------- #
# Stats from result logs
# --------------------------------------------------------------------------- #


def result_dir(cfg: Config, test_slug: str) -> Path:
    return cfg.local_repo / RESULTS_SUBDIR / slugify(cfg.effective_player) / slugify(test_slug)


def _as_int(v: object, default: int = 0) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def _as_float(v: object, default: float = 0.0) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _as_str(v: object, default: str = "") -> str:
    return v if isinstance(v, str) else default


def read_result_logs(cfg: Config, test_slug: str) -> list[dict[str, object]]:
    d = result_dir(cfg, test_slug)
    out: list[dict[str, object]] = []
    if not d.exists():
        return out
    for p in sorted(d.glob("*.toml")):
        try:
            with open(p, "rb") as f:
                out.append(tomllib.load(f))
        except (tomllib.TOMLDecodeError, OSError):
            continue  # ignore corrupt log rather than crash
    return out


def attempts_and_last_score(
    cfg: Config, test_slug: str
) -> tuple[int, tuple[int, int, int, int], tuple[int, int, int, int]]:
    """Return (attempts, last_score, best_score) where each score is a
    4-tuple: (correct, wrong, timed, total).

    Best = the session with the highest correct count; ties broken by
    earliest started_at (first to achieve that score).
    """
    logs = read_result_logs(cfg, test_slug)
    if not logs:
        return 0, (0, 0, 0, 0), (0, 0, 0, 0)

    def started(log: dict[str, object]) -> str:
        v = log.get("started_at", "")
        return v if isinstance(v, str) else ""

    logs.sort(key=started)
    last = logs[-1]
    last_score = (
        _as_int(last.get("correct")),
        _as_int(last.get("wrong")),
        _as_int(last.get("timed_out")),
        _as_int(last.get("total_questions")),
    )
    best_correct = -1
    best_score = (0, 0, 0, 0)
    for log in logs:
        c = _as_int(log.get("correct"))
        w = _as_int(log.get("wrong"))
        tm = _as_int(log.get("timed_out"))
        t = _as_int(log.get("total_questions"))
        if c > best_correct:
            best_correct = c
            best_score = (c, w, tm, t)
    return len(logs), last_score, best_score


def score_markup(score: tuple[int, int, int, int]) -> list[tuple[str | None, str] | str]:
    """Return urwid Text markup with foreground-only colours
    (correct=green, wrong=red, timeout=yellow, total=default):"""
    correct, wrong, timed, total = score
    sep = " / "
    return [
        ("light green,bold", str(correct)),
        sep,
        ("light red,bold", str(wrong)),
        sep,
        ("yellow,bold", str(timed)),
        sep,
        (None, str(total)),
    ]


# Background used for the focused list row.
FOCUS_BG = "dark blue"


# When a row is focused, AttrMap's focus_map replaces the source attribute of
# each run-length segment.  A single AttrSpec would clobber the per-character
# colours (green/red/yellow), so we use a dict keyed by every source attr the
# rows use, mapping each to a focused version that keeps the same foreground
# but paints FOCUS_BG behind it.  `None` (the "default" attr) maps to a plain
# white-bold-on-FOCUS_BG style.
def _focus_spec(fg: str) -> "urwid.AttrSpec":
    return urwid.AttrSpec(fg, FOCUS_BG)


ROW_FOCUS_MAP = {
    None: _focus_spec("white,bold"),
    "muted": _focus_spec("dark gray"),
    "light green,bold": _focus_spec("light green,bold"),
    "light red,bold": _focus_spec("light red,bold"),
    "yellow,bold": _focus_spec("yellow,bold"),
}


def focus_attr_map(label_markup: object) -> urwid.AttrMap:
    """Wrap a Text widget with a focus-map that preserves per-segment colours
    while painting FOCUS_BG behind the focused row."""
    return urwid.AttrMap(
        SelectableText(label_markup),
        None,
        focus_map=ROW_FOCUS_MAP,
    )


def wrong_counts_by_question(cfg: Config, test_slug: str) -> dict[str, int]:
    """Aggregate wrong counts per question id across all prior sessions."""
    counts: dict[str, int] = {}
    for log in read_result_logs(cfg, test_slug):
        mistakes = log.get("mistakes", [])
        if not isinstance(mistakes, list):
            continue
        for w in mistakes:
            if not isinstance(w, dict):
                continue
            qid = _as_str(w.get("question"))
            counts[qid] = counts.get(qid, 0) + 1
    return counts


def recent_tests(cfg: Config, topn: int = 5) -> list[str]:
    """Slugs most recently used by this player, newest first."""
    player_dir = cfg.local_repo / RESULTS_SUBDIR / slugify(cfg.effective_player)
    if not player_dir.exists():
        return []
    pairs: list[tuple[str, str]] = []
    for test_sub in player_dir.iterdir():
        if not test_sub.is_dir():
            continue
        newest = ""
        for log_path in test_sub.glob("*.toml"):
            try:
                with open(log_path, "rb") as f:
                    log = tomllib.load(f)
                started = _as_str(log.get("started_at"))
                if started > newest:
                    newest = started
            except Exception:
                continue
        if newest:
            pairs.append((newest, test_sub.name))
    pairs.sort(reverse=True)
    return [slug for _, slug in pairs[:topn]]


def order_questions(
    questions: list[Question], wrong_counts: dict[str, int], rng: random.Random
) -> list[Question]:
    """Most-failed first; random tiebreak among equal failure counts."""
    # We want stable random per-id ordering so that repeated sorts with the
    # same wrong_counts would still differ if ties exist — use a randomized
    # secondary key.
    jitter = {q.id: rng.random() for q in questions}
    return sorted(
        questions,
        key=lambda q: (-wrong_counts.get(q.id, 0), jitter[q.id]),
    )


# --------------------------------------------------------------------------- #
# Result log writing
# --------------------------------------------------------------------------- #


def write_result_log(
    cfg: Config,
    test: Test,
    *,
    started_at: str,
    duration: float,
    correct: int,
    wrong: int,
    timed_out: int,
    commit_id: str,
    wrongs: list[dict[str, object]],
) -> Path:
    d = result_dir(cfg, test.slug)
    d.mkdir(parents=True, exist_ok=True)
    # Use started_at to derive a filename; replace ':' for portability.
    fname = started_at.replace(":", "-")
    path = d / f"{fname}.toml"
    header = {
        "test": test.slug,
        "test_title": test.title,
        "player": cfg.effective_player,
        "commit": commit_id,
        "started_at": started_at,
        "duration_seconds": round(duration, 3),
        "total_questions": len(test.questions),
        "correct": correct,
        "wrong": wrong,
        "timed_out": timed_out,
    }
    write_toml(path, header, arrays=[("mistakes", wrongs)])
    return path


# --------------------------------------------------------------------------- #
# TUI building blocks (urwid)
# --------------------------------------------------------------------------- #

PALETTE = [
    ("title", "light cyan,bold", "default"),
    ("prompt", "yellow,bold", "default"),
    ("good", "light green,bold", "default"),
    ("bad", "light red,bold", "default"),
    ("warn", "yellow,bold", "default"),
    ("muted", "dark gray", "default"),
    ("key", "black", "light gray"),
    ("hi", "white,bold", "dark blue"),
    ("answer", "white,bold", "default"),  # no blue background
    ("bar", "dark gray", "default"),  # light/less visible
    ("bar_dim", "dark gray", "default"),  # unused bar segment
    ("bar_warn", "yellow", "default"),
    ("bar_crit", "light red", "default"),
    ("select", "black", "light green"),
    ("focus", "black", "dark cyan"),
    ("error", "light red,bold", "default"),
]


def centered(widget: object, valign: str = "middle", halign: str = "center") -> urwid.Filler:
    return urwid.Filler(
        urwid.Padding(widget, align=halign, width=("relative", 95)), valign=valign, top=1, bottom=1
    )


class TextButton(urwid.Button):
    button_left = urwid.Text("  ")
    button_right = urwid.Text("")


class QuitApp(Exception):
    pass


# --------------------------------------------------------------------------- #
# Modal screen runner
# --------------------------------------------------------------------------- #


def run_loop(
    top_widget: urwid.Widget,
    *,
    unhandled: Callable[[object], None] | None = None,
    tick_interval: float | None = None,
    tick_fn: Callable[[], None] | None = None,
) -> None:
    """Run an urwid MainLoop until `loop.stop()` is called.

    `tick_fn` (if set) is invoked roughly every `tick_interval` seconds; it
    must read/write state in closures rather than return values.
    `unhandled` is the unhandled_input handler.
    """
    loop = urwid.MainLoop(
        top_widget,
        palette=PALETTE,
        unhandled_input=unhandled,
        handle_mouse=False,
        pop_ups=False,
    )
    if tick_fn is not None and tick_interval is not None:

        def _alarm() -> None:
            tick_fn()
            loop.set_alarm_in(tick_interval, lambda *_: _alarm())

        loop.set_alarm_in(tick_interval, lambda *_: _alarm())
    loop.run()


# --------------------------------------------------------------------------- #
# Message / error screens
# --------------------------------------------------------------------------- #


def show_message(
    text: str,
    *,
    attr: str = "prompt",
    wait_keys: tuple[str, ...] = ("enter",),
    prompt_label: str = "Press Enter to continue.",
) -> str:
    """Block until one of `wait_keys` is pressed; return which key was pressed."""
    state: list[str | None] = [None]

    body = urwid.Pile(
        [
            urwid.Text((attr, text), align="center"),
            urwid.Text(""),
            urwid.Text(prompt_label, align="center"),
        ]
    )
    top = centered(urwid.LineBox(body, title="Panda"))

    def on_key(key: object) -> None:
        k = key.lower() if isinstance(key, str) else None
        if k == "q":
            raise QuitApp()
        if k is not None and k in wait_keys:
            state[0] = k
            raise urwid.ExitMainLoop()

    run_loop(top, unhandled=on_key)
    return state[0] or "enter"


# --------------------------------------------------------------------------- #
# First-run: ask for repo_url
# --------------------------------------------------------------------------- #


def prompt_repo_url() -> tuple[str, bool]:
    """Return (url, disabled). One of them is meaningful:
    - if URL entered → (url, False): clone the remote repo.
    - if user picks 'disable' (N)  → ("", True): use local files only, no git.
    - if user quits (Esc/q)        → ("", False): caller aborts.
    """
    url_holder: list[str | None] = [None]
    disabled_holder: list[bool | None] = [None]
    edit = urwid.Edit(("prompt", "Git repo URL> "), "")
    status = urwid.Text("")

    def on_change(_w: object, value: str) -> None:
        if value.strip():
            status.set_text(
                ("muted", "Enter to confirm. Press N to use local files (no git). Esc/q to quit.")
            )
        else:
            status.set_text(
                ("muted", "Press Enter after typing URL, or N to skip the repo (local-only mode).")
            )

    urwid.connect_signal(edit, "change", on_change)
    on_change(edit, "")

    body = urwid.Pile(
        [
            urwid.Text(("title", "Panda — first run"), align="center"),
            urwid.Text(""),
            urwid.Text("No repo URL configured. Paste your git SSH URL for the"),
            urwid.Text("questions/results repository, OR skip and use local"),
            urwid.Text("files only (no git, no push, no integrity history):"),
            urwid.Text(""),
            urwid.AttrMap(edit, "hi"),
            urwid.Text(""),
            status,
        ]
    )
    top = centered(urwid.LineBox(body, title="Setup"))

    def on_key(key: object) -> None:
        k = key.lower() if isinstance(key, str) else None
        if k == "q" or k == "esc":
            raise QuitApp()
        if k == "n":
            disabled_holder[0] = True
            raise urwid.ExitMainLoop()
        if k == "enter":
            v = edit.edit_text.strip()
            if v:
                url_holder[0] = v
                raise urwid.ExitMainLoop()
            # Empty Enter → treat as disable (no URL typed).
            disabled_holder[0] = True
            raise urwid.ExitMainLoop()

    run_loop(top, unhandled=on_key)
    return url_holder[0] or "", bool(disabled_holder[0])


# --------------------------------------------------------------------------- #
# Test picker (urwid ListBox with filter)
# --------------------------------------------------------------------------- #


class SelectableText(urwid.Text):
    """A Text widget that accepts focus so it can live in a ListBox."""

    _selectable = True

    def keypress(self, size: tuple[int, int], key: str) -> str | None:
        return key


def make_navigable_listbox(
    items: Any,
    on_select: Callable[[str], None] | None,
    on_new: Callable[[], None] | None = None,
    on_back: Callable[[], None] | None = None,
    extra_keys: dict[str, Callable[[], None]] | None = None,
) -> urwid.ListBox:
    """Build a ListBox with selectable rows and vim+arrow navigation.

    Returns a listbox widget. Callbacks:
    - on_select(slug)      — Enter or `l` on a row
    - on_new()             — `n` pressed anywhere (optional)
    - on_back()            — `h` or Esc pressed (optional)
    - extra_keys: dict of key→callback (optional)
    """

    class NavListBox(urwid.ListBox):
        focus_position: int

        def keypress(self, size: tuple[int, int], key: str) -> str | None:
            k = key.lower() if isinstance(key, str) else key
            if k in ("j", "down"):
                self.focus_position = min(self.focus_position + 1, len(self.body) - 1)
                return None
            if k in ("k", "up"):
                self.focus_position = max(self.focus_position - 1, 0)
                return None
            if k in ("l", "enter"):
                if on_select:
                    row = self.body[self.focus_position]
                    if hasattr(row, "row_value"):
                        on_select(row.row_value)
                return None
            if k == "h" and on_back:
                on_back()
                return None
            if k == "esc" and on_back:
                on_back()
                return None
            if k == "n" and on_new:
                on_new()
                return None
            if k == "q":
                raise QuitApp()
            if extra_keys and k in extra_keys:
                extra_keys[k]()
                return None
            return super().keypress(size, key)

    return NavListBox(items)


def pick_test(
    cfg: Config,
    tests: list[TestInfo],
    recent_slugs: list[str],
) -> tuple[str, str] | None:
    """Show a filterable list of tests with vim+arrow navigation.

    Returns: ("new", slug)  — start a new game immediately
             ("attempts", slug) — show attempts list for this test
             None — user quit
    """
    result: list[tuple[str, str] | None] = [None]
    edit = urwid.Edit(("prompt", "Filter: "), "")
    list_walker = urwid.SimpleListWalker([])

    def make_row(slug: str, title: str, attempts: int, last: Score, best: Score) -> Any:
        marker = "\u2605 " if slug in recent_slugs else "  "
        label: list[tuple[str | None, str] | str] | str
        if attempts:
            label = [
                (None, f"{marker}{title}  \u2014  attempts: {attempts}, "),
                ("muted", "last "),
                *score_markup(last),
                ("muted", "  best "),
                *score_markup(best),
            ]
        else:
            label = f"{marker}{title}  \u2014  new"
        w = focus_attr_map(label)
        w.row_value = slug
        return w

    def rebuild(filter_text: str) -> None:
        filter_text = filter_text.lower()
        list_walker.clear()
        recent_first = [s for s in recent_slugs if any(t[0] == s for t in tests)]
        rest = [t[0] for t in tests if t[0] not in recent_first]
        ordered_slugs = recent_first + rest
        by_slug = {t[0]: t for t in tests}
        for slug in ordered_slugs:
            if slug not in by_slug:
                continue
            if (
                filter_text
                and filter_text not in slug.lower()
                and filter_text not in by_slug[slug][1].lower()
            ):
                continue
            list_walker.append(make_row(*by_slug[slug]))

    def on_change(_w: object, value: str) -> None:
        rebuild(value)

    urwid.connect_signal(edit, "change", on_change)
    rebuild("")

    def _on_select(slug: str) -> None:
        result[0] = ("attempts", slug)
        raise urwid.ExitMainLoop()

    def _on_new() -> None:
        # Pick the focused test row to know which test to start.
        row = listbox.focus
        if row is not None and hasattr(row, "row_value"):
            result[0] = ("new", row.row_value)
            raise urwid.ExitMainLoop()

    def _on_back() -> None:
        result[0] = None
        raise QuitApp()

    listbox = make_navigable_listbox(
        list_walker,
        on_select=_on_select,
        on_new=_on_new,
        on_back=_on_back,
    )
    header = urwid.Pile(
        [
            urwid.AttrMap(urwid.LineBox(edit, title="Panda — pick a test"), "title"),
            urwid.Text(
                (
                    "muted",
                    "j/k or ↑/↓ to move, l/Enter to see attempts, n for new game, h/Esc/q to quit.",
                ),
                align="left",
            ),
        ]
    )
    frame = urwid.Frame(
        body=urwid.AttrMap(urwid.LineBox(listbox, title="Available tests"), None),
        header=header,
    )
    top = urwid.AttrMap(frame, None)
    run_loop(top, unhandled=lambda k: None)
    return result[0]


# --------------------------------------------------------------------------- #
# Attempts list screen
# --------------------------------------------------------------------------- #


def show_attempts(cfg: Config, test: Test) -> str | None:
    """Show all past sessions for a test.

    Returns: "new" — start a new game
             None — user went back to test list
    """
    result: list[str | None] = [None]
    logs = read_result_logs(cfg, test.slug)
    logs.sort(key=lambda lg: _as_str(lg.get("started_at")), reverse=True)

    items = urwid.SimpleListWalker([])
    for log in logs:
        started = _as_str(log.get("started_at"), "?")
        score = (
            _as_int(log.get("correct")),
            _as_int(log.get("wrong")),
            _as_int(log.get("timed_out")),
            _as_int(log.get("total_questions")),
        )
        dur = _as_float(log.get("duration_seconds"))
        label = [
            (None, f"  {started}  "),
            *score_markup(score),
            ("muted", f"  ({dur:.0f}s)"),
        ]
        w = focus_attr_map(label)
        items.append(w)

    if not items:
        items.append(urwid.AttrMap(SelectableText(("muted", "  No past sessions yet.")), None))

    def _on_new() -> None:
        result[0] = "new"
        raise urwid.ExitMainLoop()

    def _on_back() -> None:
        result[0] = None
        raise urwid.ExitMainLoop()

    listbox = make_navigable_listbox(
        items,
        on_select=lambda _: None,
        on_new=_on_new,
        on_back=_on_back,
    )
    # on_select not used here — Enter does nothing on an attempt row. But
    # we still allow `l`/Enter to pass through silently.
    header = urwid.Pile(
        [
            urwid.Text(("title", f"Attempts: {test.title}"), align="center"),
            urwid.Text(
                ("muted", "j/k or ↑/↓ to move, n for new game, h/Esc/q to go back."), align="left"
            ),
        ]
    )
    frame = urwid.Frame(
        body=urwid.AttrMap(urwid.LineBox(listbox, title="Past sessions"), None),
        header=header,
    )
    top = urwid.AttrMap(frame, None)
    run_loop(top, unhandled=lambda k: None)
    return result[0]


# --------------------------------------------------------------------------- #
# Confirm-quit prompts (modal overlays)
# --------------------------------------------------------------------------- #


def confirm_quit_modal() -> str:
    """Return 'yes' / 'no'."""
    return _yes_no_modal("Quit Panda?", "Press Y to quit, N to stay.")


def confirm_stop_test_modal() -> str:
    return _yes_no_modal("Stop current test?", "Press Y to stop, N to resume.")


def _yes_no_modal(question: str, hint: str) -> str:
    yes: list[bool | None] = [None]

    body = urwid.Pile(
        [
            urwid.Text(("warn", question), align="center"),
            urwid.Text(""),
            urwid.Text(hint, align="center"),
        ]
    )
    top = centered(urwid.LineBox(body, title="Confirm"))

    def on_key(key: object) -> None:
        k = key.lower() if isinstance(key, str) else None
        if k == "y":
            yes[0] = True
            raise urwid.ExitMainLoop()
        if k == "n":
            yes[0] = False
            raise urwid.ExitMainLoop()

    run_loop(top, unhandled=on_key)
    return "yes" if yes[0] else "no"


# --------------------------------------------------------------------------- #
# Quiz screen
# --------------------------------------------------------------------------- #


@dataclass
class QResult:
    question_id: str
    correct: bool
    reason: str | None
    chosen_answer: str | None
    took: float


@dataclass
class _Session:
    """Mutable quiz-loop state shared across the run_quiz closures."""

    i: int = 0
    loop: urwid.MainLoop | None = None
    loaded: bool = False  # a question is currently shown (has start/deadline/answers/q)
    start: float = 0.0
    deadline: float = 0.0
    timeout: int = 0
    answers: list[str] = field(default_factory=list)
    keymap: dict[str, str] = field(default_factory=dict)
    correct_idx: int = 0
    timed_out: bool = False
    result: QResult | None = None
    q: Question | None = None


def run_quiz(test: Test, ordered: list[Question], cfg: Config) -> tuple[bool, list[QResult]]:
    """Run the quiz loop interactively.

    Returns (finished_naturally, results). If user aborts via Esc-yes,
    `finished_naturally` is False (caller decides whether to save partial).
    """
    rng = random.Random()
    pool = collect_answer_pool(test)

    results: list[QResult] = []
    finished = False
    aborted = False

    header_title = urwid.Text("", align="center")
    question_text = urwid.Text("", align="left")
    timer_text = urwid.Text("", align="center")
    stats_text = urwid.Text("", align="center")
    progress_text = urwid.Text("", align="center")
    # Answers stacked top-down in a single column (one row per answer key).
    answer_pile = urwid.Pile([])
    flash_text = urwid.Text("", align="center")

    # Wrap question and answers in an 80-char box centered on screen so the
    # left edge of the question text aligns with the left edge of the answers.
    body_col = urwid.Pile(
        [
            question_text,
            urwid.Divider(),
            answer_pile,
        ]
    )
    body_box = urwid.Padding(body_col, align="center", width=80, min_width=0)

    # Centre column: title, question, answers, stats. Bars live in the Frame
    # footer so they sit at the bottom of the screen and stay unobtrusive.
    centre_pile = urwid.Pile(
        [
            urwid.AttrMap(header_title, "title"),
            urwid.Divider(),
            body_box,
            urwid.Divider(),
            stats_text,
            urwid.Divider(),
            flash_text,
        ]
    )
    centre = urwid.Filler(centre_pile, valign="middle")

    footer = urwid.Pile(
        [
            timer_text,
            progress_text,
        ]
    )

    top = urwid.Frame(body=centre, footer=footer)

    session = _Session()

    def update_stats_and_progress() -> None:
        done = session.i
        total = len(ordered)
        correct = sum(1 for r in results if r.correct)
        wrong = sum(1 for r in results if not r.correct and r.reason == "wrong")
        timed = sum(1 for r in results if not r.correct and r.reason == "time")
        left = total - done
        stats_text.set_text(
            (
                "muted",
                f"Left: {left}    \u2713 on-time: {correct}    "
                f"\u2717 wrong: {wrong}    \u2748 time: {timed}",
            )
        )
        frac = (done / total) if total else 0.0
        progress_text.set_text(render_bar(frac, width=40))

    def show_question(i: int) -> None:
        q = ordered[i]
        answers = sample_six_answers(q, pool, rng)
        # Map keys to answers: S D F / J K L (top row S D F, bottom row J K L)
        keymap = dict(zip(ANSWER_KEYS, answers, strict=True))
        correct_idx = answers.index(q.correct)
        timeout = q.timeout if q.timeout is not None else test.timeout

        session.i = i
        session.loaded = True
        session.start = time.monotonic()
        session.deadline = time.monotonic() + timeout
        session.timeout = timeout
        session.answers = answers
        session.keymap = keymap
        session.correct_idx = correct_idx
        session.timed_out = False
        session.result = None
        session.q = q

        header_title.set_text(f"Question {i + 1} of {len(ordered)}")
        question_text.set_text(("prompt", q.question))
        update_stats_and_progress()
        render_timer()
        render_answers(flash_after=None)

    def render_timer() -> None:
        s = session
        if not s.loaded:
            return
        now = time.monotonic()
        left = s.deadline - now
        timeout = s.timeout
        if left <= 0:
            left = 0
            bar_frac = 0.0
            attr = "bar_crit"
            label = "\u26a0 TIME UP"
        else:
            bar_frac = left / timeout if timeout else 0.0
            if bar_frac > 0.5:
                attr = "bar"
            elif bar_frac > 0.2:
                attr = "bar_warn"
            else:
                attr = "bar_crit"
            label = ""
            # only show numeric seconds when timeout > 5
            if timeout > 5:
                label = f"  {int(round(left))}s"
        bar_markup = list(render_bar(bar_frac, width=40, full_attr=attr, empty_attr="bar_dim"))
        if label:
            bar_markup.append((attr, label))
        timer_text.set_text(bar_markup)

    def render_answers(flash_after: QResult | None) -> None:
        s = session
        # Answers stacked top-down: one row per key, ordered S D F J K L.
        # Each row shows the key label on the left and the answer centred.
        rows: list[Any] = []
        if not s.loaded:
            answer_pile.contents.clear()
            return
        answers = s.answers
        for i, (k, a) in enumerate(zip(ANSWER_KEYS, answers, strict=True)):
            key_label = k.upper()
            # Color flash: green if this was correct, red if user picked wrong.
            if flash_after is not None:
                if i == s.correct_idx:
                    cell_attr = "good"
                elif flash_after.chosen_answer == a and not flash_after.correct:
                    cell_attr = "bad"
                else:
                    cell_attr = "answer"
            else:
                cell_attr = "answer"
            # Row: [key]  answer — two spaces between key and answer, left-aligned.
            key_w = urwid.Text(("key", f" {key_label}  "), align="left")
            disp = a[:74]  # 80 - 6 for the key tag (incl. 2-space gap)
            ans_w = urwid.Text((cell_attr, disp), align="left")
            row = urwid.Columns(
                [
                    (6, key_w),
                    ans_w,
                ]
            )
            rows.append((urwid.AttrMap(row, None), ("pack", None)))
        answer_pile.contents[:] = rows

    def set_flash(r: QResult | None) -> None:
        if r is None:
            flash_text.set_text("")
            return
        if r.correct:
            flash_text.set_text(("good", "\u2705  Correct!"))
        elif r.reason == "time":
            flash_text.set_text(("bad", "\u23f0  Time up — answer recorded as failed-on-time."))
        else:
            flash_text.set_text(("bad", "\u274c  Wrong. The correct answer was highlighted."))

    def advance_or_finish(r: QResult) -> None:
        results.append(r)
        update_stats_and_progress()
        # Show flash + correct answer highlight, then proceed after a short delay.
        render_answers(flash_after=r)
        set_flash(r)
        # Use a short alarm to let the user see the result, then advance.
        if session.loop is None:
            return  # loop not started; shouldn't happen
        loop = session.loop

        def _next() -> None:
            nonlocal finished
            next_i = session.i + 1
            if next_i >= len(ordered):
                finished = True
                raise urwid.ExitMainLoop()
            show_question(next_i)

        # Wrong answers linger 3s so the correct one can be seen; correct
        # answers flash briefly (0.8s) since nothing extra needs reading.
        delay = 3.0 if not r.correct else 0.8
        loop.set_alarm_in(delay, lambda *_: _next())

    def on_key(key: object) -> None:
        nonlocal aborted
        k = key.lower() if isinstance(key, str) else None
        if k == "q":
            ans = confirm_quit_modal()
            if ans == "yes":
                aborted = True
                raise urwid.ExitMainLoop()
            return
        if k == "esc":
            ans = confirm_stop_test_modal()
            if ans == "yes":
                aborted = True
                raise urwid.ExitMainLoop()
            return
        ak = normalize_key(key)
        if ak is None:
            return
        # Answer key — record result regardless of timed_out status.
        s = session
        q = s.q
        if not s.loaded or q is None:
            return
        if s.result is not None:
            return  # already answered (waiting for flash advance)
        now = time.monotonic()
        took = now - s.start
        chosen = s.keymap[ak]
        correct = chosen == q.correct
        if correct:
            reason = None
        else:
            reason = "time" if s.timed_out else "wrong"
        r = QResult(q.id, correct, reason, chosen if not correct else None, took)
        s.result = r
        advance_or_finish(r)

    def tick() -> None:
        s = session
        # Update timer bar.
        render_timer()
        # Check timeout.
        if not s.loaded:
            return
        if not s.timed_out and time.monotonic() >= s.deadline:
            s.timed_out = True
            # Don't auto-advance — wait for answer key. Just flip indicator.
            # (We could optionally flash a warning; the timer text already flips.)

    loop = urwid.MainLoop(top, palette=PALETTE, unhandled_input=on_key, handle_mouse=False)
    session.loop = loop

    def _alarm_tick() -> None:
        tick()
        loop.set_alarm_in(0.1, lambda *_: _alarm_tick())

    loop.set_alarm_in(0.1, lambda *_: _alarm_tick())

    # show first question
    show_question(0)
    try:
        loop.run()
    except QuitApp:
        aborted = True
    return (finished and not aborted), results


# --------------------------------------------------------------------------- #
# Finish screen + gamification
# --------------------------------------------------------------------------- #


def pick_finish_message(correct: int, wrong: int, timed: int, total: int) -> tuple[str, str]:
    """Return (attr, message)."""
    if wrong == 0 and timed == 0 and correct == total and total > 0:
        return "good", random.choice(PERFECT_MSGS)
    if wrong == 0 and timed > 0:
        return "warn", random.choice(SOME_TIMEOUT_MSGS)
    if 1 <= wrong <= 2:
        return "good", random.choice(FEW_WRONG_MSGS)
    return "bad", random.choice(HARD_MSGS)


def show_finish(
    test: Test, *, correct: int, wrong: int, timed: int, total: int, duration: float
) -> None:
    attr, msg = pick_finish_message(correct, wrong, timed, total)
    rows = [
        urwid.Text(("title", f"Test complete: {test.title}"), align="center"),
        urwid.Text(""),
        urwid.Text((attr, msg), align="center"),
        urwid.Text(""),
        urwid.Text(("muted", f"Score: {correct}/{total} on-time"), align="center"),
        urwid.Text(
            ("muted", f"Wrong: {wrong}   Timed out: {timed}   Duration: {duration:.1f}s"),
            align="center",
        ),
        urwid.Text(""),
        urwid.Text(("prompt", "Press Enter to continue, q to quit."), align="center"),
    ]
    top = centered(urwid.LineBox(urwid.Pile(rows), title="Summary"))

    def on_key(key: object) -> None:
        k = key.lower() if isinstance(key, str) else None
        if k == "q":
            raise QuitApp()
        if k == "enter":
            raise urwid.ExitMainLoop()

    run_loop(top, unhandled=on_key)


# --------------------------------------------------------------------------- #
# Pre-flight screen
# --------------------------------------------------------------------------- #


def show_preflight_error(message: str) -> str:
    """Show pre-flight failures. Return 'retry' or 'quit'."""
    action: list[str] = ["quit"]

    rows = [
        urwid.Text(("error", "Panda cannot start safely."), align="center"),
        urwid.Text(""),
        urwid.Text(message),
        urwid.Text(""),
        urwid.Text(
            "Fix the repository state, then press R to retry, or q to quit.", align="center"
        ),
    ]
    top = centered(urwid.LineBox(urwid.Pile(rows), title="Repository not ready"))

    def on_key(key: object) -> None:
        k = key.lower() if isinstance(key, str) else None
        if k == "q":
            action[0] = "quit"
            raise urwid.ExitMainLoop()
        if k == "r":
            action[0] = "retry"
            raise urwid.ExitMainLoop()

    run_loop(top, unhandled=on_key)
    return action[0]


# --------------------------------------------------------------------------- #
# Main game flow
# --------------------------------------------------------------------------- #


def list_tests(cfg: Config) -> list[TestInfo]:
    """Return list of (slug, title, attempts, last_score, best_score)."""
    tests_dir = tests_dir_for(cfg)
    out: list[TestInfo] = []
    if not tests_dir.exists():
        return out
    for p in sorted(tests_dir.glob("*.toml")):
        if p.name.endswith(".sha256"):
            continue
        try:
            test = parse_test(p)  # parse without integrity check for the picker
            attempts, last, best = attempts_and_last_score(cfg, test.slug)
            out.append((test.slug, test.title, attempts, last, best))
        except Exception:
            continue
    return out


def play(cfg: Config) -> None:
    # First-run: repo URL prompt loop.
    while not cfg.repo_url and not cfg.repo_disabled:
        url, disabled = prompt_repo_url()
        if disabled:
            cfg.repo_disabled = True
            cfg.repo_url = ""
            save_config(cfg)
            break
        if not url:
            return
        cfg.repo_url = url
        cfg.repo_disabled = False
        save_config(cfg)
        break

    # Ensure local clone exists / pulls — skipped in local-only mode.
    if not cfg.repo_disabled:
        while True:
            if not (cfg.local_repo / ".git").exists():
                try:
                    cfg.local_repo.mkdir(parents=True, exist_ok=True)
                    git(cfg.local_repo.parent, "clone", cfg.repo_url, str(cfg.local_repo))
                except subprocess.CalledProcessError as e:
                    show_message(
                        f"git clone failed:\n{e.stderr.strip()}",
                        attr="error",
                        wait_keys=("enter",),
                        prompt_label="Press Enter to retry / q to quit.",
                    )
                    continue
            else:
                try:
                    git(cfg.local_repo, "pull", "--ff-only")
                except subprocess.CalledProcessError as e:
                    show_message(
                        f"git pull failed:\n{e.stderr.strip()}",
                        attr="error",
                        wait_keys=("enter",),
                        prompt_label="Press Enter to retry / q to quit.",
                    )
                    # Don't return — let user retry; pull failure shouldn't block
                    # the picker permanently.

            # Pre-flight git sanity.
            ok, msg = git_state_is_clean(cfg.local_repo)
            if not ok:
                action = show_preflight_error(msg)
                if action == "retry":
                    continue
                return
            break
    else:
        # Local-only mode: just make sure the local dir exists so tests can be
        # dropped in by hand.
        cfg.local_repo.mkdir(parents=True, exist_ok=True)

    # Main picker loop.
    while True:
        tests = list_tests(cfg)
        if not tests:
            show_message(
                "No tests found in the repository.",
                attr="error",
                wait_keys=("enter",),
                prompt_label="Press Enter to quit.",
            )
            return
        recent = recent_tests(cfg, topn=5)
        result = pick_test(cfg, tests, recent)
        if result is None:
            return
        action, slug = result
        toml_path = tests_dir_for(cfg) / f"{slug}.toml"
        try:
            test = load_test_safely(toml_path)
        except IntegrityError as e:
            show_message(
                str(e),
                attr="error",
                wait_keys=("enter",),
                prompt_label="Press Enter to go back / q to quit.",
            )
            continue
        except Exception as e:
            show_message(
                f"Failed to parse test: {e}",
                attr="error",
                wait_keys=("enter",),
                prompt_label="Press Enter to go back / q to quit.",
            )
            continue

        if action == "attempts":
            # Show attempts list; `n` there starts a new game, otherwise back.
            sub = show_attempts(cfg, test)
            if sub != "new":
                continue

        # Run the quiz.
        wrong_counts = wrong_counts_by_question(cfg, slug)
        ordered = order_questions(test.questions, wrong_counts, random.Random())

        commit_id = "" if cfg.repo_disabled else get_commit_id(cfg.local_repo)
        started_at = iso_now()
        started_wall = time.monotonic()

        finished, results = run_quiz(test, ordered, cfg)

        duration = time.monotonic() - started_wall
        correct = sum(1 for r in results if r.correct)
        wrong = sum(1 for r in results if not r.correct and r.reason == "wrong")
        timed = sum(1 for r in results if not r.correct and r.reason == "time")
        wrongs = []
        for r in results:
            if r.correct:
                continue
            wrongs.append(
                {
                    "question": r.question_id,
                    "answer": r.chosen_answer or "",
                    "took": round(r.took, 3),
                    "reason": r.reason or "wrong",
                }
            )

        write_result_log(
            cfg,
            test,
            started_at=started_at,
            duration=duration,
            correct=correct,
            wrong=wrong,
            timed_out=timed,
            commit_id=commit_id,
            wrongs=wrongs,
        )

        if not cfg.repo_disabled:
            ok, msg = commit_and_push_results(cfg.local_repo, cfg.effective_player)
            if not ok:
                sys.stderr.write(f"panda: {msg}\n")

        if finished:
            show_finish(
                test,
                correct=correct,
                wrong=wrong,
                timed=timed,
                total=len(test.questions),
                duration=duration,
            )
        # Regardless: loop back to picker (unless q was pressed during finish).


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_init_repo(url: str) -> int:
    cfg = load_config()
    cfg.repo_url = url
    save_config(cfg)
    ensure_repo(cfg)
    print(f"Cloned {url} into {cfg.local_repo}")
    return 0


def cmd_verify_tests(tests_dir_arg: str | None) -> int:
    cfg = load_config()
    if tests_dir_arg:
        cfg.tests_dir = Path(os.path.expanduser(tests_dir_arg))
    tests_dir = tests_dir_for(cfg)
    if not tests_dir.exists():
        sys.stderr.write(f"no tests dir at {tests_dir}\n")
        return 1
    updated = 0
    for p in sorted(tests_dir.glob("*.toml")):
        if p.suffix == ".sha256":
            continue
        # skip sidecars
        side = write_sha256_sidecar(p)
        updated += 1
        print(f"{p.name}  ->  {side.name}")
    print(f"{updated} test file(s) verified/sidecars refreshed.")
    return 0


def cmd_play(tests_dir_arg: str | None) -> int:
    cfg = load_config()
    if tests_dir_arg:
        cfg.tests_dir = Path(os.path.expanduser(tests_dir_arg))
    try:
        play(cfg)
    except QuitApp:
        pass
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="panda", description="Panda — terminal quiz game")
    parser.add_argument(
        "--init", metavar="URL", help="clone URL into the configured local_repo and persist it"
    )
    parser.add_argument(
        "--verify", action="store_true", help="refresh *.sha256 sidecars for the test files"
    )
    parser.add_argument(
        "tests_dir",
        nargs="?",
        default=None,
        help="directory with *.toml test files (default: config tests_dir)",
    )
    args = parser.parse_args(argv)
    if args.init is not None:
        return cmd_init_repo(args.init)
    if args.verify:
        return cmd_verify_tests(args.tests_dir)
    return cmd_play(args.tests_dir)


if __name__ == "__main__":
    raise SystemExit(main())
