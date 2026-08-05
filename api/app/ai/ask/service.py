"""Turning a question written in English into rows out of this database.

THE MODEL NEVER ANSWERS
-----------------------
It proposes one SELECT and a plain-English account of what that SELECT does.
Everything after that is deterministic: `safety.check_sql` decides whether the
statement may run, the database plans it before it is executed, and the row
cap, the statement timeout and a read-only connection bound what a mistake can
cost. What reaches the pharmacist is rows that came out of Postgres — never a
sentence that came out of a model. This is the same bargain as `ai/intake`,
where the model transcribes an invoice and `intake/validate.py` decides whether
the transcription is trustworthy.

The one thing the model writes that nothing can check is `explanation`, and
that is why it is specified as prose a pharmacist can read rather than a
restatement of the SQL: "I looked at every batch with stock left and kept the
ones expiring before September" is a claim somebody can disagree with. "Selects
from lots joined to stock_balances" is not, and it is exactly what a model
produces when nobody asks for better.

WHY THERE IS NO RETRIEVAL AND NO AGENT FRAMEWORK
------------------------------------------------
The whole schema fits in the prompt — `schema_context.TOKEN_BUDGET` is 6000
tokens and the briefing is generated from the ORM metadata — so there is
nothing to retrieve and no vector index to keep in step with the migrations.
Retrieval here would add a component that can be wrong, to solve a problem this
schema does not have.

An agent loop is rejected for a stronger reason. The familiar text-to-SQL agent
explores: it runs a query, reads the result, and tries another. Every one of
those is a query nobody checked, against a live database, billed per turn. This
pipeline is one function, one model call, and — only when the database itself
says the SQL will not plan — exactly one repair.

MEMORY IS ONE STEP, AND THAT IS A DESIGN DECISION
-------------------------------------------------
A follow-up is given the previous question and the SQL it produced. Not a
transcript. With depth, the model starts dropping a filter set two turns ago
and returns a smaller, entirely believable number that nobody questions —
`tests/golden_questions.py` has the pairs this rule is measured against, and
its NEW cases are the quiet ones: a stale filter narrows a result, and nothing
on screen says a filter was applied.

Where the classification is genuinely unclear, this asks back. Asking back is a
correct outcome scored as strictly as a correct number, not a failure to answer.

WHAT A CLEVERLY WRITTEN QUESTION BUYS AN ATTACKER
-------------------------------------------------
The question is untrusted text and it goes into the prompt. Nothing here
pretends to defend that at the prompt level; "ignore previous instructions"
works often enough that treating prompt wording as a control would be
negligence. The defence is that the model's output is not trusted either.
Whatever it can be talked into proposing is a string that must survive
`safety.check_sql`, plan on the server, and then run as a role that cannot
write. A successful jailbreak buys the ability to propose a DROP, which is
refused unread.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any

import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.ai.ask.safety import (
    DEFAULT_ROW_CAP,
    UnsafeQuery,
    check_sql,
    customer_guard,
)
from app.ai.ask.schema_context import build_schema_context
from app.ai.intake import service as intake
from app.core.config import settings
from app.db.session import SessionLocal

log = structlog.get_logger(__name__)

#: How long a question may occupy the database. Somebody typed a sentence and
#: is watching a spinner, so this is a person's patience rather than a server's
#: capacity: past ten seconds the useful response is "narrow the question", not
#: a slower answer. It also bounds the cost no string checker can see — a
#: cartesian join is legal SQL and an honest mistake, and this is what stops it
#: becoming an outage.
STATEMENT_TIMEOUT_MS = 10_000

#: A question is a sentence. Much beyond this is a paste — usually an attempt
#: to bury an instruction at the end of one — and while the guard downstream is
#: what actually stops that, there is no reason to pay for the tokens.
MAX_QUESTION_CHARS = 500

#: Bars past this many stop being a comparison and become a table drawn
#: sideways, with labels too narrow to read.
MAX_BARS = 12

#: A separate, least-privilege Postgres role for Ask, when the deployment has
#: one. Read from the environment here rather than added to `Settings` on
#: purpose: `settings` is imported by every module in the system, and a second
#: database URL sitting on it is one attribute access away from being handed to
#: `create_engine` by code that then writes through it. The only consumer reads
#: it, and nobody else can reach it by accident.
READ_ONLY_URL = os.getenv("DATABASE_URL_RO", "").strip()


class AskError(RuntimeError):
    """Base for anything that stops a question being answered."""


class AskUnavailable(AskError):
    """No model configured — the feature is switched off, not broken."""


class AskRejected(AskError):
    """The question itself is unusable: empty, or far too long."""


class AskFailed(AskError):
    """The model or the database was reached and produced nothing usable."""


class PlanFailed(AskError):
    """The database would not plan the statement, and said why.

    Internal to the pipeline: it is what triggers the one repair, and it never
    reaches a caller — a second failure is re-raised as `AskFailed`.
    """


class Outcome(str, Enum):
    """Whether the question was answered, refused, or handed back.

    The values match `tests/golden_questions.Outcome` deliberately, so the
    accuracy harness compares this field to the answer key directly instead of
    through a translation table that could quietly map a refusal onto a pass.
    """

    ANSWER = "answer"
    REFUSE = "refuse"
    CLARIFY = "clarify"


class Mode(str, Enum):
    """How a follow-up relates to the turn before it.

    Values match `tests/golden_questions.Turn`, for the same reason.
    """

    NEW = "new"
    REFINE = "refine"


class ChartHint(str, Enum):
    """What the result should be drawn as. A hint, never a promise."""

    STAT = "stat"
    BAR = "bar"
    LINE = "line"
    TABLE = "table"


class _Kind(Enum):
    """What one column holds, as far as drawing it is concerned.

    Internal, and never serialised — unlike the three above, which cross the
    wire and are therefore pinned to strings a client can read.
    """

    NUMBER = "number"
    TIME = "time"
    TEXT = "text"


@dataclass(frozen=True)
class PreviousTurn:
    """The whole of Ask's memory: the last question and the SQL it produced.

    Two fields rather than a list of turns, because the type is where the
    one-step rule is actually enforced. A list would hold the rule in a comment
    beside it, and a comment does not survive somebody adding "just a bit more
    context" to fix the one case a longer history appears to fix.
    """

    question: str
    sql: str


@dataclass(frozen=True)
class Proposal:
    """What the model returned, read defensively. Nothing here has been run."""

    mode: Mode | None
    clarifying_question: str
    sql: str
    explanation: str
    assumptions: list[str] = field(default_factory=list)
    confidence: float | None = None


@dataclass(frozen=True)
class Repair:
    """One failed statement and the database's own words about it."""

    sql: str
    error: str


@dataclass(frozen=True)
class Answer:
    """Everything the screen needs, including the reasons it got nothing.

    A refusal and a clarification are results rather than exceptions: both have
    to be shown, and a refusal has to be shown *with* the SQL that earned it —
    `bench/ask` scores "refused after proposing a mutation" differently from
    "never proposed one", and a person deserves to see what was about to run.
    """

    outcome: Outcome
    question: str
    mode: Mode
    sql: str | None = None
    #: Carried back only on a REFINE, so the screen can show what changed
    #: between the two statements rather than asserting that something did.
    previous_sql: str | None = None
    explanation: str = ""
    assumptions: list[str] = field(default_factory=list)
    confidence: float | None = None
    clarifying_question: str | None = None
    refusal: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    #: Time in the database, not time in the model. It is the number that says
    #: whether the question wants narrowing; the model's latency is ours.
    elapsed_ms: int = 0
    chart_hint: ChartHint = ChartHint.TABLE
    summary: str = ""


# ------------------------------------------------------------------- the prompt

_INSTRUCTIONS = """\
You are writing ONE PostgreSQL SELECT that answers a pharmacist's question
about the database described above.

You do not answer the question. You propose a query; code you cannot talk to
decides whether it runs, and the pharmacist reads the rows it returns. A figure
you put in `explanation` that did not come out of the database is a lie with a
citation attached.

RULES THAT DECIDE WHETHER YOUR QUERY RUNS AT ALL. A proposal breaking any of
these is thrown away unread and the person gets nothing.

1. ONE SELECT, and nothing else. No INSERT, UPDATE, DELETE, CREATE, DROP,
   GRANT or TRUNCATE; no second statement after a semicolon; no data-modifying
   CTE; no SET, no EXPLAIN, no COPY; no pg_* function or system catalogue.
   A leading WITH is fine.
2. THE users TABLE MAY GIVE UP ONLY id, full_name AND email. Never
   password_hash, never the refresh_tokens table, and never `SELECT *` — nor
   `u.*` — in any query that mentions users at all.
3. AT MOST {cap} ROWS COME BACK, whatever your query says: it is wrapped in an
   outer SELECT with that limit before it runs. If the honest answer is a long
   list, aggregate it, or rank it and take the top few. A truncated list read
   as a complete one is a wrong answer.

WHAT TO PRODUCE

`sql`          the single SELECT. Empty string only when you are asking back.
`explanation`  what you did, in plain English, for somebody who does not read
               SQL: "I looked at every batch that still has stock, kept the
               ones expiring before 30 September, and sorted by what they are
               worth." Never a restatement of the SQL — "selects from lots
               joined to stock_balances" tells a pharmacist nothing they can
               disagree with, and being able to disagree is the whole point.
`assumptions`  every choice you made that the question did not make for you:
               which of two similar products you picked, that "this month"
               means the calendar month in Asia/Kolkata, that you counted only
               AVAILABLE stock, that you excluded reversals. One short sentence
               each. This is where a reader catches you answering a different
               question from the one they asked.
`confidence`   0 to 1. Your own reading of whether this query answers what was
               asked. Nothing is gated on it; it is shown to the reader.
`clarifying_question`  see below. Empty string when you do not need to ask.

WHEN TO ASK BACK INSTEAD OF ANSWERING

If the question has two different correct answers and nothing in it says which
was meant, put ONE short question in `clarifying_question` and leave `sql`
empty. "How much insulin do we have left?" is the shape of it: this catalogue
holds two insulins at very different prices, so adding them produces a number
that is about nothing, and picking one silently is a wrong answer with no tell.
Asking is a correct outcome, not a failure. But do not ask to be safe: a
question you can answer, answer, and put the judgement calls in `assumptions`.
"""

_FOLLOW_UP = """\
THIS IS A FOLLOW-UP. CLASSIFY IT BEFORE YOU WRITE ANY SQL.

The previous question and the SQL it produced are below. They are the entire
history you have. There is no more of it, nothing was said before them, and you
must not assume otherwise.

Fill in `mode` first:

  REFINE  the new message only makes sense against the previous one — "only the
          ones at Andheri", "and across the whole chain?", "same for
          Amoxicillin", "who supplies it?". Start from the previous SQL and
          change what the message changes; everything it does not mention
          stays. Refining can WIDEN: "and across the whole chain" removes the
          branch filter and keeps every other one. It can also discard almost
          all of it: "who supplies it?" keeps only the product, because a
          supplier relationship has no date window to inherit.
  NEW     the message stands on its own and is about something else. Write it
          from scratch. Nothing from the previous SQL survives — not a filter,
          not a join, not the shape of it.

If you genuinely cannot tell which it is, ask in `clarifying_question` instead
of picking. Guessing NEW drops a filter the person still meant. Guessing REFINE
keeps one they had moved on from, and that returns a smaller, entirely
plausible number that nobody thinks to question.

PREVIOUS QUESTION
{question}

PREVIOUS SQL
{sql}
"""

_REPAIR = """\
YOUR PREVIOUS ATTEMPT AT THIS SAME QUESTION DID NOT PLAN.

The database was asked to plan the query below and refused it. Fix exactly what
it complained about and change nothing else about what the query answers.

The statement was wrapped in `SELECT * FROM ( ... ) AS ask_result LIMIT {cap}`
before it was planned, so an error naming `ask_result` is about that wrapper
and not about your query. Return the inner SELECT alone, as you did before.

THE QUERY
{sql}

WHAT THE DATABASE SAID
{error}

The usual cause is a column or a table that does not exist under that name.
Read the schema above again and use the real one. Do not work around the error
by deleting the part of the question it came from — an answer to a smaller
question, returned as if it were the answer, is worse than no answer.
"""

#: Property order is load-bearing, not cosmetic. The model fills the fields in
#: the order they are declared, so `mode` is committed before any SQL exists to
#: reverse-engineer a classification from, and `clarifying_question` is decided
#: before the SQL rather than after — a model that has already written a query
#: never concludes it should have asked instead. `confidence` comes last
#: because it is a judgement about work already done.
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "mode": {"type": "STRING", "enum": ["NEW", "REFINE"]},
        "clarifying_question": {"type": "STRING"},
        "sql": {"type": "STRING"},
        "explanation": {"type": "STRING"},
        "assumptions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER"},
    },
    # All of them required, none nullable: an absent field would have to be
    # told apart from an empty one by every reader downstream. "Nothing to ask"
    # is the empty string, the way intake spells "no discount" as 0.
    "required": [
        "mode", "clarifying_question", "sql", "explanation",
        "assumptions", "confidence",
    ],
}


def _build_prompt(
    question: str,
    previous: PreviousTurn | None,
    repair: Repair | None,
) -> str:
    """The whole of what the model is told, assembled in a deliberate order.

    The schema briefing goes first because it is identical on every question in
    the system's life, which is what a provider's prefix cache can reuse; the
    question goes last because it is the only part that changes. The follow-up
    and repair sections sit between them, present only when they apply — an
    empty "PREVIOUS QUESTION: none" heading is an invitation to invent one.

    Nothing is said here about the caller's branch. A scoped account is refused
    by `safety.scope_guard` with a message written for a person to read, and
    teaching the model to pre-empt that would have it explain our authorisation
    rules to whoever asks.
    """
    sections = [
        build_schema_context(),
        _INSTRUCTIONS.format(cap=DEFAULT_ROW_CAP),
    ]
    if previous is not None:
        sections.append(
            _FOLLOW_UP.format(question=previous.question, sql=previous.sql)
        )
    if repair is not None:
        sections.append(
            _REPAIR.format(cap=DEFAULT_ROW_CAP, sql=repair.sql, error=repair.error)
        )
    sections.append(f"THE QUESTION\n{question}")
    return "\n\n".join(sections)


# ------------------------------------------------------------- the model call


def _ask_model(prompt: str) -> dict:
    """One structured call, through the client `ai/intake` already owns.

    Its `_request` is imported underscore and all, deliberately. A second httpx
    client here would mean two timeouts, two retry policies and two opinions
    about what a 429 means, and the pair would drift the first time one of them
    was tuned — which is how a feature ends up retrying four times against a
    paid API because somebody fixed a flaky test somewhere else.

    This is also the single seam the tests replace: everything above it,
    including the whole prompt, runs for real in CI.
    """
    try:
        return intake._parse(intake._request({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": PROPOSAL_SCHEMA,
                # The same question twice should propose the same query. A
                # reporting tool that disagrees with itself between two refreshes
                # is one nobody trusts a third time.
                "temperature": 0,
            },
        }))
    except intake.IntakeError as exc:
        raise AskFailed(f"the query could not be written: {exc}") from exc


def _read_proposal(doc: dict) -> Proposal:
    """Read the model's reply as data, because that is what it is.

    Every field is coerced rather than trusted. The response schema makes the
    shape likely, not certain, and a KeyError three frames further down would
    surface as a 500 on a question somebody asked in good faith.
    """
    try:
        mode: Mode | None = Mode(str(doc.get("mode", "")).strip().lower())
    except ValueError:
        # Not a classification. Left as None so the caller can ask back rather
        # than pick one — see `answer`, where picking is the whole failure.
        mode = None

    try:
        confidence: float | None = float(doc["confidence"])
    except (KeyError, TypeError, ValueError):
        confidence = None

    return Proposal(
        mode=mode,
        clarifying_question=str(doc.get("clarifying_question") or "").strip(),
        sql=str(doc.get("sql") or "").strip(),
        explanation=str(doc.get("explanation") or "").strip(),
        assumptions=[
            str(item).strip()
            for item in (doc.get("assumptions") or [])
            if str(item).strip()
        ],
        confidence=confidence,
    )


def _propose(
    question: str,
    *,
    previous: PreviousTurn | None = None,
    repair: Repair | None = None,
) -> Proposal:
    """Ask for one candidate query."""
    return _read_proposal(_ask_model(_build_prompt(question, previous, repair)))


# --------------------------------------------------------- the read-only path


@lru_cache(maxsize=1)
def _read_only_sessions() -> sessionmaker[Session]:
    """The factory Ask executes on, built once.

    TWO MECHANISMS, ONLY ONE OF WHICH IS A GUARANTEE
    ------------------------------------------------
    With `DATABASE_URL_RO` set, this connects as a separate role, and a role
    with no INSERT, UPDATE or DELETE granted is enforced by the server against
    a connection that cannot argue with it. That is a real guarantee: a write
    that somehow got past `safety.check_sql` fails as a permission error.

    Without it, Ask runs on the application's own connection and opens every
    transaction with `SET TRANSACTION READ ONLY`. That is a seatbelt, not a
    wall. The connection *can* write; it has merely been asked not to for the
    length of this transaction, and any code that opened its own transaction on
    the same session would escape it. It is worth having — it turns a guard bug
    into an error instead of a modified row — and it is not the same thing, so
    it is not described as though it were. A deployment that cares sets the
    second URL and grants that role SELECT only.

    The pool is deliberately small. Ask is one person waiting for one answer,
    and it must not be able to starve the connections the shop floor needs to
    dispense stock.
    """
    if not READ_ONLY_URL:
        return SessionLocal
    engine = create_engine(
        READ_ONLY_URL, pool_pre_ping=True, pool_size=2, max_overflow=2, echo=False
    )
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def _read_only_transaction() -> Iterator[Session]:
    """A transaction that cannot write and cannot run long, always rolled back.

    `SET TRANSACTION READ ONLY` has to be the first statement in the
    transaction, which is why it is issued here and not left to a caller, and
    it is issued even against the least-privilege role: it costs nothing, and
    it means the two deployments behave identically rather than only mostly.

    The timeout is interpolated rather than bound because Postgres will not
    take a parameter for a configuration setting. `int()` is what makes that
    safe, and it is not decoration — the value is a module constant today and
    the obvious next change is to make it an administrator's setting.
    """
    session = _read_only_sessions()()
    try:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(
            text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
        )
        yield session
    finally:
        # Never a commit, on either path. There is nothing to commit, and a
        # rollback on the way out means a timed-out statement leaves no aborted
        # transaction sitting on a pooled connection for the next caller.
        session.rollback()
        session.close()


def _database_message(exc: Exception) -> str:
    """What the driver said, without the wrapper SQLAlchemy puts around it.

    Postgres names the offending column and often suggests the right one, which
    is the whole value of feeding an error back. SQLAlchemy's `str()` prepends
    that message with the full statement and its parameters — text the model
    wrote and already has, at the cost of several hundred tokens on the one
    call being made to correct a mistake.
    """
    message = str(getattr(exc, "orig", exc)).strip()
    return message[:400]


def _explain(sql: str) -> None:
    """Have the database plan the statement without running it.

    A hallucinated column costs nothing here: EXPLAIN resolves every name and
    returns a plan, and the row that would have been read is never read. It is
    also the only check in this file that knows what the database actually
    contains — `safety.py` knows the grammar, `schema_context.py` knows the
    metadata, and neither of them would notice a column dropped by a migration
    that has not reached this environment.

    `explain` is a word `safety.py` refuses in a proposal, and this prepends it.
    That is not a hole: what is being prefixed here is text that has already
    passed every guard, and the guard's concern is a model asking for a query
    plan of its own, not us asking for one.
    """
    with _read_only_transaction() as session:
        # Only the statement is inside the try, so a database that is simply
        # unreachable propagates as itself rather than as PlanFailed. The
        # difference is a paid model call: PlanFailed means "your SQL is
        # wrong, here is why", and rewriting perfectly good SQL because the
        # connection dropped would spend the one repair on nothing.
        try:
            session.execute(text(f"EXPLAIN {sql}"))
        except SQLAlchemyError as exc:
            raise PlanFailed(_database_message(exc)) from exc


def _run(sql: str) -> tuple[list[str], list[list[Any]], int]:
    """Execute the capped statement. Returns columns, rows and the time taken.

    Values keep the types the driver gave them — Decimal stays Decimal, a date
    stays a date — because `chart_hint` reads those types, and because rounding
    money on the way out of a reporting tool is how two screens in the same
    system come to disagree by a rupee.
    """
    started = time.perf_counter()
    with _read_only_transaction() as session:
        try:
            result = session.execute(text(sql))
            columns = list(result.keys())
            rows = [list(row) for row in result.fetchall()]
        except SQLAlchemyError as exc:
            # It planned and then failed, so this is a timeout, an arithmetic
            # error, or a cast that only the real values could break.
            raise AskFailed(
                f"the query started and could not finish: {_database_message(exc)}"
            ) from exc
    return columns, rows, int((time.perf_counter() - started) * 1000)


# ------------------------------------------------------- shape of the result


def _kind_of(value: Any) -> _Kind:
    """Which of the three kinds a single value belongs to."""
    # bool before int, because in Python a bool IS an int. Without this, a
    # column of true/false is a measure with two values and gets plotted.
    if isinstance(value, bool):
        return _Kind.TEXT
    if isinstance(value, int | float | Decimal):
        return _Kind.NUMBER
    # datetime subclasses date, so this covers a timestamp too.
    if isinstance(value, date):
        return _Kind.TIME
    return _Kind.TEXT


def _column_kind(rows: list[list[Any]], index: int) -> _Kind:
    """A column's kind, from its first value that is not NULL.

    Reading only the first row would call a measure "text" whenever the top row
    happens to be null — common under an outer join, and it silently demotes a
    perfectly good chart to a table.
    """
    for row in rows:
        if index < len(row) and row[index] is not None:
            return _kind_of(row[index])
    return _Kind.TEXT


def chart_hint(columns: list[str], rows: list[list[Any]]) -> ChartHint:
    """Which picture this result asks for, from its shape and its types.

    Deterministic, and pointedly not a second model call. The shape of a result
    is a fact about the result; a model asked the same question twice would
    sometimes disagree with itself, and it would cost a second round trip to be
    unreliable about something that is already known. A wrong picture is also
    not free — it is a second confident claim on the same screen as the number.

    Both charted cases are exactly two columns, a label and a measure. Three
    columns with a date among them is a small table that happens to have a date
    in it, and deciding which of the others is the series would be a guess.
    """
    if not rows:
        # A chart of nothing looks broken rather than empty. The table at least
        # shows which columns were asked for.
        return ChartHint.TABLE

    kinds = [_column_kind(rows, index) for index in range(len(columns))]

    if len(columns) == 1 and len(rows) == 1 and kinds[0] is _Kind.NUMBER:
        return ChartHint.STAT
    if len(columns) == 2:
        pair = set(kinds)
        if pair == {_Kind.TEXT, _Kind.NUMBER} and len(rows) <= MAX_BARS:
            return ChartHint.BAR
        if pair == {_Kind.TIME, _Kind.NUMBER}:
            return ChartHint.LINE
    return ChartHint.TABLE


def _as_text(value: Any) -> str:
    """One value, written the way a sentence would say it."""
    if value is None:
        return "nothing"
    if isinstance(value, Decimal):
        # 240.000 reads as a precision claim nobody made. `f` rather than
        # str() so a normalised 1E+2 does not reach a pharmacist as "1E+2".
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def summarise(
    columns: list[str],
    rows: list[list[Any]],
    *,
    truncated: bool = False,
) -> str:
    """A sentence about what came back, built from the rows and only the rows.

    Not from the question, and the signature is what enforces that — the
    question is not passed in. A summary written from the question says "we
    have 412 products in stock" because that is what was asked, and it says it
    in exactly the same confident voice whether or not a single row supports
    it. A summary written from the rows can only say what is in them, and when
    there are none it can only say that.

    Which is why the empty case does not speculate. Zero rows is not evidence
    of zero stock, and "nothing is expiring" is a claim this function is not in
    a position to make.
    """
    if not rows:
        return (
            "No rows came back. Nothing matched the question as it was run, "
            "which is not the same as the answer being zero."
        )

    cap_note = (
        f" That is the {DEFAULT_ROW_CAP}-row cap, so there may be more that is "
        f"not shown."
        if truncated
        else ""
    )

    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0]}: {_as_text(rows[0][0])}.{cap_note}"

    # Four fields is a sentence; ten is the row again, and the row is already
    # on the screen underneath this.
    described = ", ".join(
        f"{name} {_as_text(value)}"
        for name, value in zip(columns[:4], rows[0][:4], strict=False)
    )
    if len(rows) == 1:
        return f"One row — {described}.{cap_note}"
    # "The first", never "the largest": the ordering belongs to the SQL, and
    # this function has not read it.
    return f"{len(rows)} rows. The first is {described}.{cap_note}"


# ------------------------------------------------------------- the pipeline


@dataclass(frozen=True)
class _Planned:
    """A statement that is ready to run, or the refusal that stopped it."""

    proposal: Proposal
    runnable: str | None
    refusal: str | None


def _gate_and_plan(sql: str, allowed_warehouse_ids: list[int] | None) -> str:
    """The guard, then the planner, in that order and never one without the other.

    Returns the capped statement as it would reach the database. Raises
    `UnsafeQuery` if it may not run at all, or `PlanFailed` if the database
    will not have it.
    """
    runnable = check_sql(sql, allowed_warehouse_ids=allowed_warehouse_ids)
    _explain(runnable)
    return runnable


def _plan(
    question: str,
    proposal: Proposal,
    *,
    previous: PreviousTurn | None,
    allowed_warehouse_ids: list[int] | None,
) -> _Planned:
    """Gate and plan the proposal, repairing it once if the database objects.

    Once, and the two attempts are written out rather than looped, because the
    number is the point. A loop against a metered API is an unbounded bill, and
    the second attempt is worth making for one specific reason: the model has
    now been told, in the database's own words, that a column it invented does
    not exist. A third attempt has nothing new to tell it.

    A refusal is returned, never repaired. `check_sql` refused the statement
    because of what it asked for, and asking a model to have another go at
    reading a password hash is a conversation with only one honest end.
    """
    error = ""
    try:
        return _Planned(
            proposal, _gate_and_plan(proposal.sql, allowed_warehouse_ids), None
        )
    except UnsafeQuery as refusal:
        return _Planned(proposal, None, str(refusal))
    except PlanFailed as broken:
        # Bound to a local: `broken` is unbound the moment this block ends.
        error = str(broken)
        log.info("ask.repairing", question=question, error=error)

    repaired = _propose(
        question, previous=previous, repair=Repair(sql=proposal.sql, error=error)
    )
    try:
        return _Planned(
            repaired, _gate_and_plan(repaired.sql, allowed_warehouse_ids), None
        )
    except UnsafeQuery as refusal:
        return _Planned(repaired, None, str(refusal))
    except PlanFailed as still_broken:
        raise AskFailed(
            f"this question could not be turned into a query the database "
            f"accepts. It said: {still_broken}. Rephrasing it, or naming the "
            f"branch or the product exactly, usually gets further."
        ) from still_broken


def _clarify(
    question: str,
    proposal: Proposal,
    asking: str,
    *,
    mode: Mode = Mode.NEW,
) -> Answer:
    """Hand the question back. Nothing is run, whatever SQL came with it.

    Including the SQL: a proposal that both asked and answered has picked one
    of the readings anyway, and running it would put a number on the screen
    beside the question about which number it is.
    """
    log.info("ask.clarifying", question=question, asking=asking)
    return Answer(
        outcome=Outcome.CLARIFY,
        question=question,
        mode=mode,
        explanation=proposal.explanation,
        assumptions=proposal.assumptions,
        confidence=proposal.confidence,
        clarifying_question=asking,
    )


def answer(
    question: str,
    *,
    allowed_warehouse_ids: list[int] | None,
    customer_id: int | None = None,
    previous: PreviousTurn | None = None,
) -> Answer:
    """Answer one question. Reads; never writes; may refuse, may ask back.

    `allowed_warehouse_ids` is what `app.core.deps.scoped_warehouse_ids`
    returns — None for a manager or an administrator, a list for a branch
    account — and it is passed straight to the guard.

    There is no `Session` parameter, and that is deliberate. Ask opens its own
    read-only transaction, so there is no writable session anywhere on this
    code path for a future edit to reach for by accident.
    """
    # Before the question is even read, and well before a model is paid: this
    # is a fact about the account, not about anything they typed.
    customer_guard(customer_id)

    asked = question.strip()
    if not asked:
        raise AskRejected("there is no question here to answer")
    if len(asked) > MAX_QUESTION_CHARS:
        raise AskRejected(
            f"that question is {len(asked)} characters; the limit is "
            f"{MAX_QUESTION_CHARS}. Ask the shortest version of it."
        )
    if not settings.gemini_api_key:
        raise AskUnavailable(
            "asking questions in plain English is not configured on this "
            "server (set GEMINI_API_KEY to enable it)"
        )

    proposal = _propose(asked, previous=previous)

    # With no previous turn there is nothing to refine, so the classification
    # is not the model's to make: it is NEW by construction, whatever it said.
    mode = Mode.NEW if previous is None else proposal.mode
    if mode is None:
        # It was asked to choose between two words and returned neither. That
        # is the one state where guessing is worst — inherit and a stale filter
        # narrows the answer invisibly; start clean and a filter the person
        # still meant is dropped — so the question goes back to them.
        return _clarify(
            asked,
            proposal,
            "I could not tell whether you meant to narrow the previous "
            "question or start a new one. Which is it?",
        )

    if proposal.clarifying_question:
        return _clarify(asked, proposal, proposal.clarifying_question, mode=mode)

    if not proposal.sql:
        raise AskFailed(
            "the model proposed neither a query to run nor a question to ask "
            "back, so there is nothing to show"
        )

    plan = _plan(
        asked,
        proposal,
        previous=previous,
        allowed_warehouse_ids=allowed_warehouse_ids,
    )
    if plan.runnable is None:
        log.warning("ask.refused", question=asked, reason=plan.refusal)
        return Answer(
            outcome=Outcome.REFUSE,
            question=asked,
            mode=mode,
            sql=plan.proposal.sql,
            explanation=plan.proposal.explanation,
            assumptions=plan.proposal.assumptions,
            confidence=plan.proposal.confidence,
            refusal=plan.refusal,
        )

    columns, rows, elapsed_ms = _run(plan.runnable)
    truncated = len(rows) >= DEFAULT_ROW_CAP
    log.info(
        "ask.answered",
        mode=mode.value,
        row_count=len(rows),
        elapsed_ms=elapsed_ms,
        truncated=truncated,
    )
    return Answer(
        outcome=Outcome.ANSWER,
        question=asked,
        # The classification from the first pass. A repair rewrites SQL; it
        # does not get to change its mind about what was being asked.
        mode=mode,
        sql=plan.proposal.sql,
        previous_sql=previous.sql if mode is Mode.REFINE and previous else None,
        explanation=plan.proposal.explanation,
        assumptions=plan.proposal.assumptions,
        confidence=plan.proposal.confidence,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=elapsed_ms,
        chart_hint=chart_hint(columns, rows),
        summary=summarise(columns, rows, truncated=truncated),
    )
