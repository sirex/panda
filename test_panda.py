"""Pytest tests for panda.py.

Uses only stdlib + pytest (per the chosen test approach). Imports panda.py
via importlib so the file's unusual name (no 'package') works regardless of
how pytest discovers it.
"""

import importlib.util
import random
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("panda", HERE / "panda.py")
panda = importlib.util.module_from_spec(SPEC)
sys.modules["panda"] = panda
SPEC.loader.exec_module(panda)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def rng():
    return random.Random(0)


@pytest.fixture
def tmp_repo(tmp_path):
    """A bare-bones fake local_repo dir; tests populate tests/ and results/."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "results").mkdir()
    return repo


def write_test_file(repo: Path, name: str, body: str, with_sidecar: bool = True) -> Path:
    p = repo / "tests" / f"{name}.toml"
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    if with_sidecar:
        panda.write_sha256_sidecar(p)
    return p


def make_config(repo_path: Path, player: str = "tester") -> panda.Config:
    return panda.Config(player=player, repo_url="", local_repo=repo_path)


# --------------------------------------------------------------------------- #
# parse_test / validation
# --------------------------------------------------------------------------- #


def test_parse_test_valid(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "math-easy",
        """
        title = "Math — Easy"
        timeout = 10

        [[questions]]
        id = "q1"
        question = "What is 7 × 8?"
        correct = "56"
        answers = ["54","56","64","48","63","42"]

        [[questions]]
        id = "q2"
        question = "What is 15 + 9?"
        correct = "24"
        timeout = 5
    """,
    )
    t = panda.parse_test(p)
    assert t.slug == "math-easy"
    assert t.title == "Math — Easy"
    assert t.timeout == 10
    assert len(t.questions) == 2
    assert t.questions[0].id == "q1"
    assert t.questions[0].answers == ["54", "56", "64", "48", "63", "42"]
    assert t.questions[1].answers is None
    assert t.questions[1].timeout == 5


def test_parse_test_missing_answers(tmp_repo, rng):
    p = write_test_file(
        tmp_repo,
        "x",
        """
        title = "X"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q1"
        correct = "A"
        [[questions]]
        id = "q2"
        question = "Q2"
        correct = "B"
    """,
    )
    t = panda.parse_test(p)
    pool = panda.collect_answer_pool(t)
    assert "A" in pool and "B" in pool
    answers = panda.sample_six_answers(t.questions[0], pool, rng)
    assert t.questions[0].correct in answers
    assert len(answers) == 6
    assert len(set(answers)) == 6


def test_parse_rejects_duplicate_answers(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "bad",
        """
        title = "Bad"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
        answers = ["A","A","A","A","A","A"]
    """,
    )
    with pytest.raises(ValueError, match="duplicate answers"):
        panda.parse_test(p)


def test_parse_rejects_correct_not_in_answers(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "bad",
        """
        title = "Bad"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "Z"
        answers = ["A","B","C","D","E","F"]
    """,
    )
    with pytest.raises(ValueError, match="'correct' not in answers"):
        panda.parse_test(p)


def test_parse_rejects_wrong_answer_count(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "bad",
        """
        title = "Bad"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
        answers = ["A","B","C"]
    """,
    )
    with pytest.raises(ValueError, match="must have 6 answers"):
        panda.parse_test(p)


def test_parse_rejects_duplicate_ids(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "bad",
        """
        title = "Bad"
        timeout = 10
        [[questions]]
        id = "dup"
        question = "Q1"
        correct = "A"
        [[questions]]
        id = "dup"
        question = "Q2"
        correct = "B"
    """,
    )
    with pytest.raises(ValueError, match="duplicate question id"):
        panda.parse_test(p)


def test_parse_rejects_no_questions(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "empty",
        """
        title = "Empty"
        timeout = 10
    """,
    )
    with pytest.raises(ValueError, match="no questions"):
        panda.parse_test(p)


# --------------------------------------------------------------------------- #
# Integrity / sha256
# --------------------------------------------------------------------------- #


def test_missing_sidecar_refused(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "x",
        """
        title = "X"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
        with_sidecar=False,
    )
    assert panda.verify_sha256_sidecar(p) is False
    with pytest.raises(panda.IntegrityError):
        panda.load_test_safely(p)


def test_tampered_toml_refused(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "x",
        """
        title = "X"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
    )
    # Mutate after sidecar was written.
    p.write_text(p.read_text().replace('"A"', '"B"'), encoding="utf-8")
    # Sidecar still matches the original; file no longer matches.
    assert panda.verify_sha256_sidecar(p) is False
    with pytest.raises(panda.IntegrityError):
        panda.load_test_safely(p)


def test_write_sidecar_roundtrips(tmp_repo):
    p = write_test_file(
        tmp_repo,
        "x",
        """
        title = "X"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
    )
    assert panda.verify_sha256_sidecar(p) is True


# --------------------------------------------------------------------------- #
# Question ordering
# --------------------------------------------------------------------------- #


def test_order_questions_most_failed_first():
    qs = [
        panda.Question(id="a", question="?", correct="a"),
        panda.Question(id="b", question="?", correct="b"),
        panda.Question(id="c", question="?", correct="c"),
    ]
    wrong = {"a": 0, "b": 5, "c": 3}
    ordered = panda.order_questions(qs, wrong, random.Random(0))
    ids = [q.id for q in ordered]
    assert ids[0] == "b"  # most wrong
    assert ids[1] == "c"
    assert ids[2] == "a"


def test_order_questions_tiebreak_is_random():
    qs = [panda.Question(id=str(i), question="?", correct=str(i)) for i in range(4)]
    wrong = {q.id: 0 for q in qs}  # all tied
    # Run many orderings; "a first" pattern should show some variation.
    firsts = set()
    for seed in range(200):
        ordered = panda.order_questions(qs, wrong, random.Random(seed))
        firsts.add(ordered[0].id)
    assert len(firsts) > 1


# --------------------------------------------------------------------------- #
# sample_six_answers
# --------------------------------------------------------------------------- #


def test_sample_six_answers_includes_correct_and_distinct(rng):
    pool = ["A", "B", "C", "D", "E", "F"]
    q = panda.Question(id="q", question="?", correct="A")
    answers = panda.sample_six_answers(q, pool, rng)
    assert "A" in answers
    assert len(answers) == 6
    assert len(set(answers)) == 6


def test_sample_six_answers_explicit_passthrough(rng):
    pool = ["A", "B", "C", "D", "E", "F"]
    explicit = ["A", "B", "C", "D", "E", "Z"]
    q = panda.Question(id="q", question="?", correct="A", answers=explicit)
    answers = panda.sample_six_answers(q, pool, rng)
    assert answers == explicit


# --------------------------------------------------------------------------- #
# Result log round-trip
# --------------------------------------------------------------------------- #


def test_write_and_read_result_log(tmp_repo):
    cfg = make_config(tmp_repo, "alex")
    p = write_test_file(
        tmp_repo,
        "math-easy",
        """
        title = "Math"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
    )
    t = panda.parse_test(p)
    wrongs = [
        {"question": "q1", "answer": "B", "took": 8.5, "reason": "wrong"},
        {"question": "q2", "answer": "", "took": 10.0, "reason": "time"},
    ]
    out = panda.write_result_log(
        cfg,
        t,
        started_at="2026-08-05T13:00:00Z",
        duration=120.0,
        correct=1,
        wrong=1,
        timed_out=1,
        commit_id="abc123",
        wrongs=wrongs,
    )
    assert out.exists()
    with open(out, "rb") as f:
        log = tomllib.load(f)
    assert log["test"] == "math-easy"
    assert log["test_title"] == "Math"
    assert log["player"] == "alex"
    assert log["commit"] == "abc123"
    assert log["started_at"] == "2026-08-05T13:00:00Z"
    assert log["correct"] == 1
    assert log["wrong"] == 1
    assert log["timed_out"] == 1
    assert log["total_questions"] == 1
    w = log["mistakes"]
    assert len(w) == 2
    assert w[0]["question"] == "q1"
    assert w[0]["reason"] == "wrong"
    assert w[1]["reason"] == "time"


# --------------------------------------------------------------------------- #
# Stats scanning
# --------------------------------------------------------------------------- #


def test_attempts_last_and_best_score(tmp_repo):
    cfg = make_config(tmp_repo, "alex")
    p = write_test_file(
        tmp_repo,
        "math-easy",
        """
        title = "Math"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
    )
    t = panda.parse_test(p)
    panda.write_result_log(
        cfg,
        t,
        started_at="2026-08-01T10:00:00Z",
        duration=60.0,
        correct=3,
        wrong=2,
        timed_out=1,
        commit_id="x",
        wrongs=[],
    )
    panda.write_result_log(
        cfg,
        t,
        started_at="2026-08-05T10:00:00Z",
        duration=80.0,
        correct=5,
        wrong=0,
        timed_out=0,
        commit_id="y",
        wrongs=[],
    )
    attempts, last, best = panda.attempts_and_last_score(cfg, "math-easy")
    assert attempts == 2
    assert last == (5, 0, 0, 1)  # most recent: correct=5,wrong=0,timed=0,total=1
    assert best == (5, 0, 0, 1)  # best correct is also 5 (>= 3)


def test_attempts_last_and_best_score_low_last(tmp_repo):
    # Best is a prior session, last is worse.
    cfg = make_config(tmp_repo, "alex")
    p = write_test_file(
        tmp_repo,
        "math-easy",
        """
        title = "Math"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
    )
    t = panda.parse_test(p)
    panda.write_result_log(
        cfg,
        t,
        started_at="2026-08-01T10:00:00Z",
        duration=60.0,
        correct=8,
        wrong=0,
        timed_out=0,
        commit_id="x",
        wrongs=[],
    )
    panda.write_result_log(
        cfg,
        t,
        started_at="2026-08-05T10:00:00Z",
        duration=80.0,
        correct=2,
        wrong=5,
        timed_out=1,
        commit_id="y",
        wrongs=[],
    )
    attempts, last, best = panda.attempts_and_last_score(cfg, "math-easy")
    assert attempts == 2
    assert last == (2, 5, 1, 1)
    assert best == (8, 0, 0, 1)


def test_score_markup_colors(tmp_repo):
    markup = panda.score_markup((3, 2, 1, 6))
    # foreground-only colour specs, plain " / " separators
    assert markup == [
        ("light green,bold", "3"),
        " / ",
        ("light red,bold", "2"),
        " / ",
        ("yellow,bold", "1"),
        " / ",
        (None, "6"),
    ]


def test_wrong_counts_by_question(tmp_repo):
    cfg = make_config(tmp_repo, "alex")
    p = write_test_file(
        tmp_repo,
        "math-easy",
        """
        title = "Math"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
        [[questions]]
        id = "q2"
        question = "Q"
        correct = "B"
    """,
    )
    t = panda.parse_test(p)
    panda.write_result_log(
        cfg,
        t,
        started_at="2026-08-01T10:00:00Z",
        duration=60.0,
        correct=0,
        wrong=2,
        timed_out=0,
        commit_id="x",
        wrongs=[
            {"question": "q1", "answer": "B", "took": 5.0, "reason": "wrong"},
            {"question": "q1", "answer": "C", "took": 5.0, "reason": "wrong"},
        ],
    )
    panda.write_result_log(
        cfg,
        t,
        started_at="2026-08-05T10:00:00Z",
        duration=60.0,
        correct=1,
        wrong=1,
        timed_out=0,
        commit_id="y",
        wrongs=[
            {"question": "q1", "answer": "B", "took": 5.0, "reason": "wrong"},
            {"question": "q2", "answer": "C", "took": 5.0, "reason": "wrong"},
        ],
    )
    counts = panda.wrong_counts_by_question(cfg, "math-easy")
    assert counts == {"q1": 3, "q2": 1}


def test_recent_tests(tmp_repo):
    cfg = make_config(tmp_repo, "alex")
    pa = write_test_file(
        tmp_repo,
        "math-easy",
        """
        title = "Math"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
    )
    pb = write_test_file(
        tmp_repo,
        "geo-eu",
        """
        title = "Geo"
        timeout = 10
        [[questions]]
        id = "q1"
        question = "Q"
        correct = "A"
    """,
    )
    ta = panda.parse_test(pa)
    tb = panda.parse_test(pb)
    panda.write_result_log(
        cfg,
        tb,
        started_at="2026-08-01T10:00:00Z",
        duration=60.0,
        correct=1,
        wrong=0,
        timed_out=0,
        commit_id="x",
        wrongs=[],
    )
    panda.write_result_log(
        cfg,
        ta,
        started_at="2026-08-05T10:00:00Z",
        duration=60.0,
        correct=1,
        wrong=0,
        timed_out=0,
        commit_id="y",
        wrongs=[],
    )
    recent = panda.recent_tests(cfg, topn=5)
    # newest first → math-easy (later started_at) before geo-eu
    assert recent == ["math-easy", "geo-eu"]


# --------------------------------------------------------------------------- #
# Key handling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "inp,exp",
    [
        ("s", "s"),
        ("S", "s"),
        ("D", "d"),
        ("j", "j"),
        ("L", "l"),
        ("a", None),
        ("enter", None),
        ("esc", None),
        ("q", None),
        ("ctrl c", None),
    ],
)
def test_normalize_key(inp, exp):
    assert panda.normalize_key(inp) == exp


def test_answer_keys_six():
    assert len(panda.ANSWER_KEYS) == 6
    assert panda.ANSWER_KEYS_LOWER == ("s", "d", "f", "j", "k", "l")


# --------------------------------------------------------------------------- #
# render_bar
# --------------------------------------------------------------------------- #


def test_render_bar_extremes():
    full = panda.render_bar(1.0, width=10)
    assert full == [("bar", panda.BAR_FULL * 10)]
    empty = panda.render_bar(0.0, width=10)
    assert empty == [("bar_dim", panda.BAR_EMPTY * 10)]
    half = panda.render_bar(0.5, width=10)
    assert half == [("bar", panda.BAR_FULL * 5), ("bar_dim", panda.BAR_EMPTY * 5)]


def test_render_bar_clamps():
    assert panda.render_bar(-1.0, width=4) == [("bar_dim", panda.BAR_EMPTY * 4)]
    assert panda.render_bar(2.0, width=4) == [("bar", panda.BAR_FULL * 4)]


# --------------------------------------------------------------------------- #
# slugify / toml escape
# --------------------------------------------------------------------------- #


def test_slugify_basic():
    assert panda.slugify("Math — Easy!") == "math-easy"
    assert panda.slugify("  Geo  EU  ") == "geo-eu"
    assert panda.slugify("a---b") == "a-b"


def test_toml_escape_quotes():
    assert panda.toml_escape('he said "hi"') == '"he said \\"hi\\""'


def test_toml_value_types():
    assert panda.toml_value(5) == "5"
    assert panda.toml_value(5.5) == "5.5"
    assert panda.toml_value(True) == "true"
    assert panda.toml_value(False) == "false"


# --------------------------------------------------------------------------- #
# git_state_is_clean (real git in tmp dir)
# --------------------------------------------------------------------------- #


def _git_init(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "p@local"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "P"], check=True)


def test_git_clean_check_on_clean_main(tmp_path):
    repo = tmp_path / "r"
    _git_init(repo)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    ok, msg = panda.git_state_is_clean(repo)
    assert ok, msg


def test_git_clean_check_dirty_worktree(tmp_path):
    repo = tmp_path / "r"
    _git_init(repo)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    (repo / "f.txt").write_text("y")  # dirty
    ok, _msg = panda.git_state_is_clean(repo)
    assert not ok


def test_git_clean_check_wrong_branch(tmp_path):
    repo = tmp_path / "r"
    _git_init(repo)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(repo), "branch", "feat"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "feat"], check=True, capture_output=True)
    ok, _msg = panda.git_state_is_clean(repo)
    assert not ok


def test_get_commit_id(tmp_path):
    repo = tmp_path / "r"
    _git_init(repo)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    cid = panda.get_commit_id(repo)
    assert len(cid) == 40 and all(c in "0123456789abcdef" for c in cid)


def test_commit_and_push_results_commits_locally(tmp_path):
    repo = tmp_path / "r"
    _git_init(repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / "results").mkdir()
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    # add a result file
    (repo / "results" / "alex").mkdir(parents=True)
    (repo / "results" / "alex" / "x.toml").write_text(
        'test = "x"\nplayer = "alex"\n', encoding="utf-8"
    )
    ok, msg = panda.commit_and_push_results(repo, "alex")
    # push will fail (no remote); we still expect commit to succeed first,
    # so 'ok' will be False but status will mention push failure.
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True, check=True
    )
    assert "alex" in log.stdout


# --------------------------------------------------------------------------- #
# Theme / config roundtrip
# --------------------------------------------------------------------------- #


def test_config_theme_default(tmp_path, monkeypatch):
    monkeypatch.setattr(panda, "CONFIG_DIR", tmp_path / "cfg_dir")
    monkeypatch.setattr(panda, "CONFIG_PATH", tmp_path / "cfg_dir" / "config.toml")
    cfg = panda.load_config()
    assert cfg.theme == "dark"


def test_config_theme_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(panda, "CONFIG_DIR", tmp_path / "cfg_dir")
    monkeypatch.setattr(panda, "CONFIG_PATH", tmp_path / "cfg_dir" / "config.toml")
    cfg = panda.Config(theme="dark")
    panda.save_config(cfg)
    loaded = panda.load_config()
    assert loaded.theme == "dark"


def test_config_theme_load_unknown_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(panda, "CONFIG_DIR", tmp_path / "cfg_dir")
    monkeypatch.setattr(panda, "CONFIG_PATH", tmp_path / "cfg_dir" / "config.toml")
    cfg_dir = tmp_path / "cfg_dir"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text('theme = "nope"\n', encoding="utf-8")
    cfg = panda.load_config()
    assert cfg.theme == "dark"


def test_config_theme_resolve_unknown():
    t = panda._resolve_theme("nope")
    assert t.name == "dark"
