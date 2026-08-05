"""Asking a question in plain English.

    POST /ai/ask   one question, answered with rows out of this database

THE MODEL DOES NOT ANSWER THIS REQUEST
--------------------------------------
It proposes one SELECT. `ai/ask/safety.py` decides whether that statement may
run, the database plans it before it is executed, and it executes inside a
read-only transaction under a statement timeout and a row cap. Everything that
comes back is either rows Postgres returned or a sentence about a query the
asker can read — which is why `sql` is on the response even when the statement
was refused, and why a refusal is a 200 rather than a 403.

That last point is the one worth stating outright: a refusal and a question
back are outcomes, not errors. Both mean the system worked — a mutation was
declined before it reached the database, or an ambiguity was handed back
instead of being guessed at — and returning them as failures would have every
client draw them in red beside a stack trace.

WHY A POST FOR SOMETHING THAT ONLY READS
----------------------------------------
GET is the honest verb here, and the question is the reason it is not used. A
question is a sentence somebody typed about their own stock, and a follow-up
carries the previous statement along with it; in a query string both would be
copied into access logs, proxy logs and browser history by every layer between
this process and the browser. A body is read once and kept nowhere.

WHAT THIS FILE ADDS TO THE GUARD UNDERNEATH IT
----------------------------------------------
The caller's branch, and nothing else. Raw SQL walks straight past every
authorisation rule the rest of the application enforces through the ORM, so
`scoped_warehouse_ids` — the same function `/transfers` and `/adjustments`
scope on — is passed through to `safety.scope_guard` unchanged. A branch
account is then refused any table holding another branch's rows, in words it
can act on.

There is no `db` parameter, which is deliberate rather than an oversight. The
service opens its own read-only transaction, so there is no writable session
anywhere on this path for a later edit to reach for by accident.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.ai.ask import service
from app.ai.ask.schemas import AskIn, AskOut
from app.core.deps import require_feature, require_permission, scoped_warehouse_ids
from app.core.errors import ExternalServiceError, ValidationError
from app.models.identity import User

router = APIRouter(prefix="/ai/ask", tags=["AI · ask"])

#: Gated on `ai.view`, not on the `ai.ask` code that has sat in the catalogue
#: since Layer 2 was only a sketch. `ai.ask` is granted to staff and customers,
#: and `safety.scope_guard` refuses a branch-scoped account every table that
#: carries a warehouse — which is most of what anyone asks about. Offering
#: those accounts a screen that could only refuse them is worse than not
#: offering it, so Ask is gated where the rest of Analysis is gated.
VIEW = require_permission("ai.view")

#: The administrator's off switch, enforced here rather than only hidden in the
#: sidebar: switching a capability off has to mean the endpoint refuses, or a
#: stale tab carries on spending the model budget.
LIVE = require_feature("features.nl_reporting")


@router.post("", response_model=AskOut)
def ask_question(
    payload: AskIn,
    user: User = Depends(VIEW),
    _live: None = Depends(LIVE),
) -> AskOut:
    """Answer one question. Reads; never writes; may refuse, may ask back."""
    # Rebuilt as the service's own frozen type rather than passed through as the
    # request model. Two fields, and the type is where the one-step memory rule
    # lives — a request body that grew a list of turns would have to be changed
    # here before it could reach the prompt.
    previous = (
        service.PreviousTurn(
            question=payload.previous.question, sql=payload.previous.sql
        )
        if payload.previous is not None
        else None
    )

    try:
        answer = service.answer(
            payload.question,
            allowed_warehouse_ids=scoped_warehouse_ids(user),
            previous=previous,
        )
    except service.AskRejected as exc:
        # An empty question or an essay. The asker can fix it themselves, and
        # the message says how, so it is theirs rather than the server's.
        raise ValidationError(str(exc)) from exc
    except (service.AskUnavailable, service.AskFailed) as exc:
        # One shape for both, mapped exactly as `ai/intake` maps its own pair:
        # to a caller, "no model configured" and "the model produced nothing
        # usable" are the same fact — this server could not answer, and no
        # rephrasing on their side will change that.
        raise ExternalServiceError(str(exc)) from exc

    return AskOut(
        outcome=answer.outcome.value,
        question=answer.question,
        mode=answer.mode.value,
        sql=answer.sql,
        previous_sql=answer.previous_sql,
        explanation=answer.explanation,
        assumptions=answer.assumptions,
        confidence=answer.confidence,
        clarifying_question=answer.clarifying_question,
        refusal=answer.refusal,
        columns=answer.columns,
        rows=answer.rows,
        row_count=answer.row_count,
        truncated=answer.truncated,
        elapsed_ms=answer.elapsed_ms,
        chart_hint=answer.chart_hint.value,
        summary=answer.summary,
    )
