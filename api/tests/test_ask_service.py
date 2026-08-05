"""The question pipeline (app/ai/ask/service.py).

WHAT BREAKS IF THESE FAIL
-------------------------
Per class, because they cost different things:

  a refusal that runs        the model was allowed to write to the ledger, or
                             to read a column of password hashes. Nothing else
                             in this file matters if that one regresses.
  a repair that loops        every extra attempt is another metered call on a
                             question that has already failed twice, and the
                             bill is discovered at the end of the month.
  a follow-up that inherits  a NEW question carrying an old filter returns a
                             smaller, entirely plausible number, and nothing on
                             the screen says a filter was applied.
  a wrong chart hint         cosmetic on its own, and a second confident claim
                             beside a number it may not describe.

Nothing here makes a network call or needs a database. Two seams are replaced:
`_ask_model`, which is the single outbound request, and `_explain`/`_run`,
which are the only two functions that touch Postgres. Everything between them —
the prompt, the classification, the safety gate, the repair budget, the chart
hint, the summary — is the real code, so a test passing means that code ran.

The safety gate itself is not re-tested here; `test_ask_safety.py` covers it
route by route. What is tested here is that the pipeline calls it, believes it,
and stops.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.ai.ask import service
from app.ai.ask.safety import DEFAULT_ROW_CAP
from app.ai.ask.service import (
    Answer,
    AskFailed,
    AskRejected,
    AskUnavailable,
    ChartHint,
    Mode,
    Outcome,
    PreviousTurn,
    answer,
    chart_hint,
    summarise,
)
from tests.golden_questions import Outcome as KeyOutcome
from tests.golden_questions import Turn as KeyTurn

#: A question with no trap in it, used wherever the question is not the subject.
A_QUESTION = "How many products do we still stock?"

#: The first turn of the canonical refine pair from the answer key.
EXPIRING = PreviousTurn(
    question="Which batches expire in the next 60 days?",
    sql=(
        "SELECT p.sku, l.lot_code, l.expiry_date FROM lots l "
        "JOIN products p ON p.id = l.product_id "
        "WHERE l.expiry_date < CURRENT_DATE + 60"
    ),
)


def proposal(**overrides: Any) -> dict:
    """One model reply, in the shape `PROPOSAL_SCHEMA` asks for."""
    reply = {
        "mode": "NEW",
        "clarifying_question": "",
        "sql": "SELECT count(*) FROM products WHERE is_active",
        "explanation": "I counted the products that have not been retired.",
        "assumptions": ["Retired products are excluded."],
        "confidence": 0.9,
    }
    reply.update(overrides)
    return reply


class Pipeline:
    """The service with both of its outside edges replaced.

    `replies` is consumed one call at a time and running out is a failed
    assertion rather than a repeat, which is how the repair budget is enforced:
    a test that allows two model calls fails loudly on the third instead of
    passing while the pipeline quietly loops.

    `planned` and `executed` record the statements that reached each stage, so
    "never executed" can be asserted as a fact rather than inferred from an
    outcome.
    """

    def __init__(self) -> None:
        self.replies: list[dict] = []
        self.prompts: list[str] = []
        self.plan_errors: list[str] = []
        self.planned: list[str] = []
        self.executed: list[str] = []
        self.columns: list[str] = ["count"]
        self.rows: list[list[Any]] = [[412]]

    def model(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        assert self.replies, (
            "the pipeline called the model more times than this test allowed"
        )
        return self.replies.pop(0)

    def explain(self, sql: str) -> None:
        self.planned.append(sql)
        if self.plan_errors:
            raise service.PlanFailed(self.plan_errors.pop(0))

    def run(self, sql: str) -> tuple[list[str], list[list[Any]], int]:
        self.executed.append(sql)
        return self.columns, self.rows, 7


@pytest.fixture
def pipeline(monkeypatch) -> Pipeline:
    harness = Pipeline()
    monkeypatch.setattr(service.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(service, "_ask_model", harness.model)
    monkeypatch.setattr(service, "_explain", harness.explain)
    monkeypatch.setattr(service, "_run", harness.run)
    return harness


def ask(
    question: str = A_QUESTION,
    *,
    previous: PreviousTurn | None = None,
    scope: list[int] | None = None,
) -> Answer:
    """One question through the real pipeline. `scope` None is a manager."""
    return answer(question, allowed_warehouse_ids=scope, previous=previous)


# ------------------------------------------------------- a refusal is a full stop


def test_a_proposal_that_writes_is_refused_and_never_reaches_the_database(pipeline):
    """The answer key's first REFUSE case, phrased as an instruction.

    The refusal is structural rather than good behaviour by the model: what
    came back is not a single SELECT, so the guard rejects it before any
    connection is opened. `planned` being empty is the assertion that matters —
    even EXPLAIN is a round trip against a statement nobody has vetted.
    """
    pipeline.replies = [proposal(sql="DELETE FROM stock_movements WHERE id = 12")]

    result = ask("Write off the two boxes of ORS that got soaked at Bandra.")

    assert result.outcome is Outcome.REFUSE
    assert pipeline.planned == []
    assert pipeline.executed == []
    assert "delete" in result.refusal.lower()


def test_a_refusal_carries_back_the_query_that_earned_it(pipeline):
    """A query that was written and then declined is a different fact from one
    that was never written, and only the recorded SQL tells them apart."""
    pipeline.replies = [proposal(sql="DELETE FROM stock_movements WHERE id = 12")]

    result = ask("Write off the ORS at Bandra.")

    assert result.sql == "DELETE FROM stock_movements WHERE id = 12"
    assert result.explanation  # what it thought it was doing survives too


def test_a_question_reaching_for_a_credential_is_refused(pipeline):
    """A legal, well-formed SELECT that a statement-shape check waves through."""
    pipeline.replies = [proposal(sql="SELECT password_hash FROM users WHERE id = 1")]

    result = ask("What is the admin user's password hash?")

    assert result.outcome is Outcome.REFUSE
    assert "password_hash" in result.refusal
    assert pipeline.executed == []


def test_a_branch_account_is_refused_a_chain_wide_table(pipeline):
    """Scope is not the model's business and it is not asked about it — the
    guard refuses, and the refusal says where the branch's own figures live."""
    pipeline.replies = [proposal(sql="SELECT SUM(qty_on_hand) FROM stock_balances")]

    result = ask("How much stock does the chain hold?", scope=[2])

    assert result.outcome is Outcome.REFUSE
    assert "one branch" in result.refusal
    assert pipeline.executed == []


def test_a_refusal_is_not_argued_with(pipeline):
    """No repair after a refusal. The statement was declined for what it asked
    for, and asking a model to have another go at a password hash is a
    conversation with only one honest end — `replies` holding a single entry is
    what makes a second call fail this test."""
    pipeline.replies = [proposal(sql="SELECT password_hash FROM users")]

    ask("What is the admin user's password hash?")

    assert len(pipeline.prompts) == 1


# ------------------------------------------------------------ exactly one repair


def test_a_plan_failure_is_repaired_exactly_once(pipeline):
    """A hallucinated column fails at EXPLAIN, for free, and is fixed on the
    one retry. Two model calls, and the second one is allowed to succeed."""
    pipeline.replies = [
        proposal(sql="SELECT expiry FROM lots"),
        proposal(sql="SELECT expiry_date FROM lots"),
    ]
    pipeline.plan_errors = ['column "expiry" does not exist']

    result = ask("When does our stock expire?")

    assert result.outcome is Outcome.ANSWER
    assert len(pipeline.prompts) == 2
    assert result.sql == "SELECT expiry_date FROM lots"


def test_the_repair_is_told_the_database_s_own_words(pipeline):
    """The error is the entire value of the second call. Postgres names the
    column and often suggests the right one; a generic "that did not work"
    leaves the model guessing at the same schema it just guessed at."""
    pipeline.replies = [
        proposal(sql="SELECT expiry FROM lots"),
        proposal(sql="SELECT expiry_date FROM lots"),
    ]
    pipeline.plan_errors = ['column "expiry" does not exist']

    ask("When does our stock expire?")

    repair_prompt = pipeline.prompts[1]
    assert 'column "expiry" does not exist' in repair_prompt
    assert "SELECT expiry FROM lots" in repair_prompt


def test_a_second_plan_failure_gives_up_instead_of_trying_again(pipeline):
    """The retry budget is one because the second call has something new to
    say and a third has nothing. A loop here is an unbounded bill."""
    pipeline.replies = [
        proposal(sql="SELECT a FROM lots"),
        proposal(sql="SELECT b FROM lots"),
    ]
    pipeline.plan_errors = ['column "a" does not exist', 'column "b" does not exist']

    with pytest.raises(AskFailed, match='column "b" does not exist'):
        ask("When does our stock expire?")

    assert len(pipeline.prompts) == 2
    assert pipeline.executed == []


def test_a_repair_that_comes_back_unsafe_is_refused_not_repaired_again(pipeline):
    """The repair goes through the same gate as the first attempt. It is a
    fresh proposal from the same model, and a second pass over it would be the
    loop this design refuses."""
    pipeline.replies = [
        proposal(sql="SELECT expiry FROM lots"),
        proposal(sql="DROP TABLE lots"),
    ]
    pipeline.plan_errors = ['column "expiry" does not exist']

    result = ask("When does our stock expire?")

    assert result.outcome is Outcome.REFUSE
    assert len(pipeline.prompts) == 2
    assert pipeline.executed == []


# ------------------------------------------------------------- what actually runs


def test_the_database_runs_the_capped_statement_and_the_screen_shows_the_model_s(
    pipeline,
):
    """Two different strings, and both matter. The cap has to be on the text
    the server sees, and the person checking the answer has to be shown what
    was asked for rather than the wrapper this module put around it."""
    pipeline.replies = [proposal(sql="SELECT sku FROM products")]

    result = ask("Which products do we list?")

    assert pipeline.planned == pipeline.executed
    assert f"LIMIT {DEFAULT_ROW_CAP}" in pipeline.executed[0]
    assert "SELECT sku FROM products" in pipeline.executed[0]
    assert result.sql == "SELECT sku FROM products"


def test_the_model_is_given_the_schema_briefing_and_the_question(pipeline):
    """The briefing is the whole of what the model knows about this database.
    Dropping it produces plausible SQL against an imagined schema, which fails
    at EXPLAIN and burns the repair on a problem no repair can fix."""
    pipeline.replies = [proposal()]

    ask("Which bins in the central warehouse are cold chain?")

    prompt = pipeline.prompts[0]
    assert "TABLE stock_balances" in prompt
    assert "Which bins in the central warehouse are cold chain?" in prompt


def test_a_result_at_the_row_cap_says_so_rather_than_looking_complete(pipeline):
    """A capped list read as a complete one is a wrong answer with no tell."""
    pipeline.columns = ["sku"]
    pipeline.rows = [[f"SKU-{n}"] for n in range(DEFAULT_ROW_CAP)]
    pipeline.replies = [proposal(sql="SELECT sku FROM products")]

    result = ask("Which products do we list?")

    assert result.truncated is True
    assert str(DEFAULT_ROW_CAP) in result.summary
    assert result.row_count == DEFAULT_ROW_CAP


def test_an_answer_carries_the_working_out_back_with_it(pipeline):
    """The explanation and the assumptions are the only way a reader catches a
    confident answer to a different question."""
    pipeline.replies = [proposal()]

    result = ask()

    assert result.outcome is Outcome.ANSWER
    assert result.explanation == "I counted the products that have not been retired."
    assert result.assumptions == ["Retired products are excluded."]
    assert result.confidence == 0.9
    assert result.elapsed_ms == 7
    assert result.columns == ["count"]
    assert result.rows == [[412]]


# --------------------------------------------------------- one step of memory


def test_a_refinement_carries_the_previous_sql_back_for_the_screen(pipeline):
    """The canonical refine: the second turn is not a sentence on its own."""
    pipeline.replies = [
        proposal(mode="REFINE", sql=f"{EXPIRING.sql} AND w.code = 'BR-AND'")
    ]

    result = ask("Only the ones at Andheri.", previous=EXPIRING)

    assert result.mode is Mode.REFINE
    assert result.previous_sql == EXPIRING.sql
    assert result.outcome is Outcome.ANSWER


def test_a_new_question_discards_the_previous_turn_entirely(pipeline):
    """The quiet failure runs the other way: a NEW question that keeps an old
    filter returns a smaller number, and a smaller number never looks wrong."""
    pipeline.replies = [proposal(mode="NEW", sql="SELECT count(*) FROM products")]

    result = ask("How many products do we stock?", previous=EXPIRING)

    assert result.mode is Mode.NEW
    assert result.previous_sql is None


def test_the_model_is_shown_one_previous_turn_and_nothing_older(pipeline):
    """One step, and the shape of the prompt is where that is enforced."""
    pipeline.replies = [proposal(mode="REFINE", sql=EXPIRING.sql)]

    ask("Only the ones at Andheri.", previous=EXPIRING)

    prompt = pipeline.prompts[0]
    assert EXPIRING.question in prompt
    assert EXPIRING.sql in prompt
    assert prompt.count("PREVIOUS QUESTION") == 1


def test_without_a_previous_turn_the_model_does_not_get_to_classify(pipeline):
    """There is nothing to refine, so REFINE is not an available answer — the
    code decides this rather than believing the field."""
    pipeline.replies = [proposal(mode="REFINE")]

    result = ask()

    assert result.mode is Mode.NEW
    assert result.previous_sql is None
    assert "PREVIOUS QUESTION" not in pipeline.prompts[0]


def test_a_follow_up_the_model_did_not_classify_is_handed_back(pipeline):
    """Asked to choose between two words, it returned neither. This is the one
    state where every guess is bad — inherit and a stale filter narrows the
    answer invisibly, start clean and a filter they still meant is dropped."""
    pipeline.replies = [proposal(mode="possibly a refinement", sql="SELECT 1")]

    result = ask("and at Andheri?", previous=EXPIRING)

    assert result.outcome is Outcome.CLARIFY
    assert pipeline.planned == []
    assert pipeline.executed == []


# --------------------------------------------------------------- asking back


def test_an_ambiguous_question_is_handed_back_and_nothing_runs(pipeline):
    """The answer key's CLARIFY case. Both readings return a number, and both
    numbers are wrong in a way nothing on the screen would show."""
    pipeline.replies = [
        proposal(
            sql="",
            clarifying_question=(
                "Which insulin — Glargine 100IU or Human Insulin 40IU?"
            ),
        )
    ]

    result = ask("How much insulin do we have left?")

    assert result.outcome is Outcome.CLARIFY
    assert result.clarifying_question.startswith("Which insulin")
    assert pipeline.planned == []
    assert pipeline.executed == []


def test_a_question_that_asks_back_is_not_also_answered(pipeline):
    """A proposal that asks *and* answers has already picked one reading. Running
    it would put a number beside the question of which number it is."""
    pipeline.replies = [
        proposal(
            sql="SELECT SUM(qty_on_hand) FROM stock_balances",
            clarifying_question="Which insulin did you mean?",
        )
    ]

    result = ask("How much insulin do we have left?")

    assert result.outcome is Outcome.CLARIFY
    assert result.sql is None
    assert pipeline.executed == []


def test_a_proposal_with_neither_a_query_nor_a_question_fails_honestly(pipeline):
    """An empty draft that looks like an answer with no rows in it is worse
    than saying nothing came back."""
    pipeline.replies = [proposal(sql="", clarifying_question="")]

    with pytest.raises(AskFailed, match="neither"):
        ask()


# ------------------------------------------------------------- the chart hint


@pytest.mark.parametrize(
    "columns,rows,expected",
    [
        # One row, one measure: the number is the answer.
        (["on_hand"], [[Decimal("240.000")]], ChartHint.STAT),
        # A label and a measure, few enough to read the labels.
        (["branch", "units"], [["Andheri", 120], ["Bandra", 90]], ChartHint.BAR),
        (
            ["branch", "units"],
            [[f"Branch {n}", n] for n in range(service.MAX_BARS)],
            ChartHint.BAR,
        ),
        # One past the limit: bars too narrow to label are a table.
        (
            ["branch", "units"],
            [[f"Branch {n}", n] for n in range(service.MAX_BARS + 1)],
            ChartHint.TABLE,
        ),
        # A date and a measure is a trend, however few points there are.
        (
            ["day", "units"],
            [[date(2026, 8, 1), 12], [date(2026, 8, 2), 9]],
            ChartHint.LINE,
        ),
        (
            ["posted", "units"],
            [[datetime(2026, 8, 1, 9, 30), 12]],
            ChartHint.LINE,
        ),
        # Three columns is a small table that happens to have a date in it.
        (
            ["day", "sku", "units"],
            [[date(2026, 8, 1), "MET-500", 12]],
            ChartHint.TABLE,
        ),
        # A flag is a label, not a measure — bool is an int in Python.
        (["branch", "is_cold_chain"], [["Andheri", True]], ChartHint.TABLE),
        # Two measures have no label to plot against.
        (["ordered", "received"], [[100, 90]], ChartHint.TABLE),
        # One row, one label: there is no number to enlarge.
        (["sku"], [["MET-500"]], ChartHint.TABLE),
        # A null in the first row must not demote the column to text.
        (
            ["branch", "units"],
            [["Andheri", None], ["Bandra", 90]],
            ChartHint.BAR,
        ),
        # Zero rows is always a table, whatever shape it would have had.
        (["count"], [], ChartHint.TABLE),
        (["day", "units"], [], ChartHint.TABLE),
    ],
)
def test_the_chart_hint_follows_the_shape_of_the_result(columns, rows, expected):
    """Decided from the result, never by a second model call — a picture that
    disagrees with the number beside it is two claims where there was one."""
    assert chart_hint(columns, rows) is expected


# ---------------------------------------------------------------- the summary


def test_an_empty_result_is_reported_as_empty_and_nothing_more():
    """Zero rows is not evidence of zero stock, and this is exactly where a
    summary written from the question would say "we have none left"."""
    text = summarise(["units"], [])

    assert "No rows" in text
    assert "0" not in text


def test_a_single_value_is_read_back_with_its_column_name():
    assert summarise(["on_hand"], [[Decimal("240.000")]]) == "on_hand: 240."


def test_a_list_is_summarised_by_its_first_row_not_by_its_largest():
    """The ordering belongs to the SQL and this function has not read it, so
    "the first" is the only claim it can make truthfully."""
    text = summarise(["branch", "units"], [["Andheri", 120], ["Bandra", 90]])

    assert text.startswith("2 rows. The first is branch Andheri, units 120.")


def test_a_capped_list_says_so_in_the_summary():
    rows = [[f"SKU-{n}"] for n in range(DEFAULT_ROW_CAP)]

    text = summarise(["sku"], rows, truncated=True)

    assert f"{DEFAULT_ROW_CAP}-row cap" in text


# ------------------------------------------------------------- being switched off


def test_without_a_key_the_feature_reports_itself_off(monkeypatch):
    """Unconfigured is a supported state: a 503 the router can explain, not a
    crash, and not an empty result that reads as "there is nothing"."""
    monkeypatch.setattr(service.settings, "gemini_api_key", "")

    with pytest.raises(AskUnavailable, match="not configured"):
        ask()


def test_an_empty_question_is_rejected(pipeline):
    with pytest.raises(AskRejected, match="no question"):
        ask("   ")

    assert pipeline.prompts == []


def test_an_essay_is_rejected_before_anybody_pays_for_it(pipeline):
    """Much past a sentence is a paste, usually with an instruction buried at
    the end of it. The guard is what stops that mattering; this stops us
    paying for the tokens."""
    with pytest.raises(AskRejected, match="limit"):
        ask("x" * (service.MAX_QUESTION_CHARS + 1))

    assert pipeline.prompts == []


# -------------------------------------------------------- speaking to the key


def test_the_pipeline_and_the_answer_key_use_the_same_words():
    """`bench/ask` compares these fields to `tests/golden_questions.py`
    directly. If the two vocabularies drift, the scorer needs a translation
    table, and a translation table is somewhere a refusal can be mapped onto a
    pass by a typo nobody reviews."""
    assert {outcome.value for outcome in Outcome} == {
        outcome.value for outcome in KeyOutcome
    }
    assert {mode.value for mode in Mode} == {turn.value for turn in KeyTurn}
