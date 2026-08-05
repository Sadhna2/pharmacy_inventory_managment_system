"""Request and response shapes for asking a question in plain English.

The response carries three things that are usually left out of a text-to-SQL
API, and each is here because leaving it out is what makes such a tool
untrustworthy:

  `sql`          the statement that produced these rows, always, including on a
                 refusal. An answer nobody can check is an answer nobody should
                 act on, and the person best placed to spot the wrong join is a
                 pharmacist who can at least see that a branch was named.
  `assumptions`  the choices the question did not make. This is where a reader
                 catches a confident answer to a different question.
  `previous_sql` on a follow-up, the statement being refined, so the screen can
                 show what changed rather than claim something did.

`rows` is a list of lists rather than a list of objects on purpose: SQL columns
are ordered and may repeat a name, and a dict would silently drop the second
`name` in a two-table join.
"""

from typing import Any

from pydantic import BaseModel, Field


class PreviousTurnIn(BaseModel):
    """The previous question and the SQL it produced. The whole of the memory.

    Both fields or neither. The client sends back what it was given rather than
    the server keeping a conversation, which is what keeps the memory one step
    deep: there is no place for a third turn to accumulate.
    """

    question: str = Field(min_length=1, max_length=500)
    sql: str = Field(min_length=1)


class AskIn(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        description="One question, in ordinary English.",
    )
    previous: PreviousTurnIn | None = Field(
        None,
        description="Present only when this message is a follow-up. Send the "
                    "immediately preceding turn and nothing older — a longer "
                    "history makes the model drop an earlier filter and answer "
                    "confidently anyway.",
    )


class AskOut(BaseModel):
    """The answer, the refusal or the question back. Always one of the three."""

    outcome: str = Field(
        description="answer | refuse | clarify. A refusal and a question back "
                    "are correct outcomes, not errors — a mutation declined "
                    "and an ambiguity handed back are both successes."
    )
    question: str = Field(description="The question as it was understood.")
    mode: str = Field(
        description="new | refine. How this turn was read against the previous "
                    "one. Always 'new' when no previous turn was sent."
    )

    sql: str | None = Field(
        None,
        description="The statement proposed, recorded even when it was refused "
                    "— a query that was written and then declined is a "
                    "different fact from one that was never written.",
    )
    previous_sql: str | None = Field(
        None,
        description="The statement this one refined. Sent only on a refine, so "
                    "the screen can show the two side by side.",
    )
    explanation: str = Field(
        "",
        description="What the query does, in plain English rather than a "
                    "restatement of the SQL.",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Choices the question left open and the query settled.",
    )
    confidence: float | None = Field(
        None,
        description="The model's own 0-to-1 reading of its answer. Shown, "
                    "never acted on: it is a self-report, and a threshold on "
                    "it would look like a rule and behave like a coin toss.",
    )
    clarifying_question: str | None = Field(
        None, description="Set when the question was handed back instead of answered."
    )
    refusal: str | None = Field(
        None,
        description="Why the query was not allowed to run, in words the asker "
                    "can act on.",
    )

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(
        default_factory=list,
        description="Row-major, in the column order above.",
    )
    row_count: int = 0
    truncated: bool = Field(
        False,
        description="The row cap was reached, so there may be more. A capped "
                    "list read as a complete one is a wrong answer.",
    )
    elapsed_ms: int = Field(
        0, description="Time in the database. The model's latency is not the asker's."
    )
    chart_hint: str = Field(
        "table",
        description="stat | bar | line | table, decided from the shape and "
                    "column types of the result rather than by asking a model.",
    )
    summary: str = Field(
        "",
        description="One sentence about the rows that came back — built from "
                    "them, never from the question.",
    )
