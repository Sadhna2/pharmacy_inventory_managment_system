"""Deciding whether SQL a model wrote is allowed to run.

Ask never lets the model answer anything. It lets the model propose exactly one
SELECT, and this file — with no model in the loop — decides whether that SELECT
reaches the database. Every rule below is a string check with a yes or no
answer, because the moment a rule needs judgement it becomes something that can
be talked round, and the thing doing the talking is the same model whose output
is being checked.

A bug in `app/ai/intake/validate.py` produces a wrong number on a screen. A bug
here produces another branch's stock ledger, or a table of password hashes. So
this file is written to fail in one direction only: every check that cannot
decide refuses. A refused question costs the asker a rephrase; a permitted one
cannot be taken back.

WHAT THIS LAYER IS AND IS NOT
-----------------------------
It is the layer that knows things the database cannot. Postgres will happily
serve `users.password_hash` to a connection with SELECT rights, and it has no
idea that the person asking is pinned to one branch — authorisation lives in
the application (see `app/core/deps.py`), and raw SQL walks straight past all
of it. That is what the scope and credential guards exist for.

It is NOT the only thing standing between a bad query and the data, and it must
not be treated as such. The connection Ask runs on has to be a read-only role
with a `statement_timeout`. A string checker can always be out-lawyered by some
corner where its reading of the grammar and Postgres's differ; a role that
cannot write makes a DROP that slipped through a syntax error rather than an
outage, and the timeout catches the costs no parser can see — a cartesian join,
or a `generate_series` over a trillion rows, neither of which is forbidden here
because both are legitimate in an honest query.

WHY THE SCOPE GUARD REFUSES RATHER THAN REWRITES
------------------------------------------------
The obvious alternative is to add `AND warehouse_id = 3` to the query. It is
wrong twice over. Adding a predicate to arbitrary SQL correctly — through
joins, subqueries, unions, aggregates in the SELECT list — is the hard half of
writing a query planner, and getting it 95% right means 5% of answers silently
describe the wrong warehouse. And even done perfectly it answers a different
question from the one that was asked, without saying so. A refusal the asker
can read is worth more than a number they cannot check.

THE ORDER THE CHECKS RUN
------------------------
Shape, then credentials, then scope, then the row cap. The cap is applied last
and by wrapping, so every other guard inspects exactly the text the model
wrote rather than something this module has already rearranged.
"""

from __future__ import annotations

import re
from functools import cache

from app.db.base import Base
from app.models import RefreshToken, User

#: Rows an answer may carry back. This is a number a person reads in a browser,
#: not an export — past a couple of hundred rows the answer to "which products
#: are short" has stopped being an answer and become a spreadsheet, and the
#: honest response is a narrower question.
DEFAULT_ROW_CAP = 200

#: The only columns of `users` a question may name. Asking who received a
#: delivery is an ordinary reporting question and has to keep working, so the
#: three fields that identify a person stay open; everything else about an
#: account — what it may do, where it works, when it last signed in — is not
#: something the reporting tool needs and is exactly what an attacker does.
PERMITTED_USER_COLUMNS = frozenset({"id", "full_name", "email"})
#: Rendered once, because every refusal about `users` has to end by saying the
#: same thing — a refusal that does not say what *is* allowed just gets retried.
_PERMITTED_PHRASE = ", ".join(sorted(PERMITTED_USER_COLUMNS))

#: Words that may not appear anywhere in a query, at any depth. Anywhere is the
#: operative word: a data-modifying CTE (`WITH x AS (INSERT ... RETURNING ...)`)
#: is a perfectly ordinary Postgres statement that begins with the word WITH,
#: so checking only how the statement opens catches nothing.
#:
#: `into` is here because `SELECT ... INTO t` creates a table. `nextval` and
#: `setval` are here because a sequence is state and both move it. `refresh`,
#: `analyze`, `cluster` and friends do not modify rows but do modify the
#: server, and none of them belong in an answer to a question.
#:
#: When this list is wrong it is wrong in the safe direction: a future column
#: named `comment` would make some questions unanswerable, which someone will
#: notice and fix, rather than making a dangerous one answerable.
_FORBIDDEN_WORDS = frozenset({
    # writes
    "insert", "update", "delete", "truncate", "merge", "into",
    "nextval", "setval",
    # schema and privileges
    "drop", "alter", "create", "grant", "revoke", "comment", "security",
    # session and server state
    "set", "reset", "discard", "load", "listen", "notify", "unlisten",
    "vacuum", "analyze", "reindex", "cluster", "checkpoint", "refresh",
    "current_setting", "set_config",
    # transaction control — a question runs inside one, it does not steer it
    "begin", "commit", "rollback", "savepoint",
    # anything that runs something else, or moves data outside the query
    "copy", "import", "do", "call", "execute", "prepare", "deallocate",
    "explain", "lock",
    "query_to_xml", "query_to_xmlschema", "table_to_xml", "database_to_xml",
    "information_schema",
})

#: Identifier prefixes that reach past this application into the server. One
#: prefix rather than a list of names, because the list is the part that rots:
#: `pg_sleep`, `pg_read_file`, `pg_ls_dir` and `pg_stat_activity` are the ones
#: worth naming today, and `pg_authid` and `pg_shadow` hold Postgres's own
#: password hashes — but the next release ships more of them, and a rule that
#: has to be updated to stay correct is a rule that will be out of date.
#: Nothing this schema owns starts with any of these.
_FORBIDDEN_PREFIXES = ("pg_", "lo_", "dblink")

_UNTERMINATED = (
    "there is an unterminated quote or comment in this query, so it cannot be "
    "read safely — every check below would be reading different text from the "
    "text the database would run"
)

#: An identifier as Postgres spells one, lowercased by the time it is matched.
_WORD = re.compile(r"[a-z_][a-z0-9_$]*")
#: A dotted chain — `u.full_name`, or `public.users.password_hash` — tolerating
#: the whitespace a formatter might leave around the dots. The whole chain is
#: matched rather than one pair, so that a schema-qualified column still has an
#: owner: it is the second-to-last name that says which table a column is from,
#: and that attribution is what the users check runs on.
_DOTTED = re.compile(r"\b[a-z_][a-z0-9_$]*(?:\s*\.\s*[a-z_][a-z0-9_$]*)+")
#: `t.*`, and a bare `*` where a select list expands a row. Deliberately not
#: "contains a star": `qty * rate` is multiplication and `count(*)` counts rows,
#: and neither hands back a column nobody named. `\s*` rather than `\s+` after
#: SELECT because `select*from users` is valid and would otherwise slip past.
_QUALIFIED_STAR = re.compile(r"\b[a-z_][a-z0-9_$]*\s*\.\s*\*")
_BARE_STAR = re.compile(r"(?:\bselect\s*(?:distinct\s+|all\s+)?|,\s*)\*")
#: The table name followed by whatever it was aliased to.
_USERS_ALIAS = re.compile(rf"\b{User.__tablename__}\s+(?:as\s+)?([a-z_][a-z0-9_$]*)")
#: Words that can follow a table name without being an alias for it.
_NOT_AN_ALIAS = frozenset({
    "on", "using", "where", "group", "order", "limit", "offset", "having",
    "join", "inner", "left", "right", "full", "cross", "natural", "lateral",
    "union", "except", "intersect", "window", "fetch", "for", "tablesample",
    "as", "and", "or", "select", "from",
})
#: What may stand immediately before a table name or an alias for it to be a
#: name being introduced rather than a row being read. A comma is pointedly not
#: on this list: it stands before a table in `FROM receipts, users`, but it also
#: stands before one in `SELECT id, users`, which in Postgres returns the entire
#: row. Telling those apart is parsing, so comma joins onto `users` are refused
#: and the message says to write JOIN.
_BINDS_A_NAME = frozenset({"from", "join", "as", "."})


class UnsafeQuery(Exception):
    """A refusal, carrying the reason in words the asker can act on.

    The message has two readers and both matter. The person who asked needs to
    know why they got nothing back, and the model gets the same sentence on its
    one retry — so "unsafe query" would be useless to both, while "goods_receipts
    carries every branch's rows" tells one of them to ask a manager and the
    other to try a different table.
    """


# ------------------------------------------------------- reading the SQL safely


def _strip_noise(sql: str) -> str:
    """Blank out comments and string literals, keep everything else in place.

    This runs before any keyword check, and it is the only reason those checks
    are worth anything. `SELECT 1; /*x*/ DROP TABLE lots` reads as an innocent
    SELECT to anything that looks at the first word, and a query whose second
    statement is hidden behind a comment is exactly what a jailbroken model
    produces when asked to be sneaky.

    Three details are load-bearing:

      * A comment collapses to a *space*, never to nothing. Postgres treats a
        comment as whitespace, so `DR/**/OP` is two tokens and a syntax error
        rather than DROP — deleting the comment instead would invent a keyword
        that was never there and refuse an honest query.
      * Block comments nest, because they nest in Postgres. Reading them any
        other way means this module and the server disagree about where the
        code is, and every disagreement is a hiding place.
      * A double-quoted identifier keeps its contents and loses its quotes, so
        `SELECT "password_hash"` is still visibly about the password hash. It
        is consumed as a unit rather than re-scanned, or `"a--"` would open a
        comment that swallowed the rest of the select list.

    An unterminated quote or comment is refused outright. It is the one input
    where this scanner's idea of the text and the server's are guaranteed to
    differ, and guessing which of us is right is not a decision to make here.

    Two Postgres string forms are knowingly not understood: dollar quoting
    (`$$...$$`) and E-strings (`E'\\n'`). Their contents are read as code, so a
    query using either is likely to be refused for something it does not really
    say. That is the direction to be wrong in — understanding them would mean
    hiding text from the checks, and every byte hidden is somewhere to hide.
    """
    out: list[str] = []
    i, end = 0, len(sql)
    while i < end:
        pair = sql[i:i + 2]
        if pair == "--":
            newline = sql.find("\n", i)
            i = end if newline < 0 else newline
            out.append(" ")
        elif pair == "/*":
            depth, i = 1, i + 2
            while i < end and depth:
                nested = sql[i:i + 2]
                if nested == "/*":
                    depth, i = depth + 1, i + 2
                elif nested == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise UnsafeQuery(_UNTERMINATED)
            out.append(" ")
        elif sql[i] == "'":
            i, closed = i + 1, False
            while i < end:
                if sql[i] != "'":
                    i += 1
                elif sql[i:i + 2] == "''":
                    i += 2          # a doubled quote is one character of text
                else:
                    i, closed = i + 1, True
                    break
            if not closed:
                raise UnsafeQuery(_UNTERMINATED)
            out.append(" ")
        elif sql[i] == '"':
            i, closed = i + 1, False
            while i < end:
                if sql[i] != '"':
                    out.append(sql[i])
                    i += 1
                elif sql[i:i + 2] == '""':
                    i += 2
                else:
                    i, closed = i + 1, True
                    break
            if not closed:
                raise UnsafeQuery(_UNTERMINATED)
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _readable(sql: str) -> str:
    """The query as the checks below see it: no comments, no literals, folded.

    Folding to lower case is how `SeLeCt` and `DrOp` are dealt with, and it is
    correct rather than merely convenient: Postgres folds unquoted identifiers
    itself. It also means a quoted `"PASSWORD_HASH"` — which would not resolve
    on the server at all — is still refused, which is the right way round.
    """
    return _strip_noise(sql).lower()


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _preceding_token(text: str, position: int) -> str:
    """The identifier or single character immediately before `position`.

    Enough context to tell `FROM users u` from `row_to_json(u)` without
    parsing, which is the whole job of the whole-row check.
    """
    before = text[:position].rstrip()
    match = re.search(r"([a-z_][a-z0-9_$]*|.)$", before)
    return match.group(1) if match else ""


# ------------------------------------------------------------- one SELECT only


def _check_statement_shape(sql: str) -> None:
    """Refuse anything that is not exactly one read-only SELECT."""
    text = _readable(sql)

    # Splitting on `;` only works because the literals are already gone — a
    # semicolon inside `WHERE name = 'a;b'` is text, not a statement boundary.
    statements = [part for part in text.split(";") if part.strip()]
    if not statements:
        raise UnsafeQuery("there is no query here to run")
    if len(statements) > 1:
        raise UnsafeQuery(
            f"{len(statements)} statements were proposed, and a question is "
            "answered by exactly one SELECT — everything after the first "
            "semicolon is refused"
        )

    # A leading WITH is fine and common: the model builds a CTE and selects
    # from it. What matters is that the statement does not *begin* with
    # anything else; what it contains is the next check's business.
    opening = statements[0].lstrip(" \t\r\n(")
    if not re.match(r"(select|with)\b", opening):
        first = (_words(opening) or ["nothing"])[0]
        raise UnsafeQuery(
            f"a question may only run a SELECT, and this one begins with "
            f"`{first}`"
        )

    for word in _words(text):
        if word in _FORBIDDEN_WORDS:
            raise UnsafeQuery(
                f"`{word}` cannot appear in a question — Ask may only read, "
                f"and it may only read one SELECT"
            )
        if word.startswith(_FORBIDDEN_PREFIXES):
            raise UnsafeQuery(
                f"`{word}` reaches into the database server itself rather than "
                f"this application's tables, which a question may never do"
            )


def is_single_select(sql: str) -> bool:
    """Is this exactly one SELECT, with nothing in it that writes or escapes?

    A predicate for callers that only want a yes or no — checking a prompt's
    own worked examples, say. The pipeline should call `check_sql`, which runs
    this same rule and raises the reason rather than discarding it: "no" is not
    something you can put in front of a person or feed back to a model.
    """
    try:
        _check_statement_shape(sql)
    except UnsafeQuery:
        return False
    return True


# ------------------------------------------------------------------- the cap


def enforce_row_limit(sql: str, cap: int) -> str:
    """Wrap the query so it cannot return more than `cap` rows.

    Wrapping rather than editing. Finding and shrinking a query's own LIMIT
    means understanding which LIMIT it is — the one on the outer query, or the
    one inside the CTE, or the one in the subquery in the WHERE clause — and
    the cost of getting that wrong is that `LIMIT 5000000` runs. An outer
    SELECT bounds all of them at once, whatever the query is shaped like, and
    the argument for it fits in a sentence.

    A newline before the closing bracket is not formatting. A query ending in
    `-- comment` with no trailing newline would otherwise comment out the
    bracket and the cap along with it.
    """
    if cap < 1:
        raise UnsafeQuery(f"a row cap of {cap} would return nothing")
    inner = sql.strip().rstrip(";").rstrip()
    # int() rather than trust: the cap reaches this from an administrator's
    # settings row, and one that arrived as a string would be a second place
    # to inject SQL — the one place in this file that writes any.
    return f"SELECT * FROM (\n{inner}\n) AS ask_result LIMIT {int(cap)}"


# --------------------------------------------------------------- branch scoping


@cache
def scoped_tables() -> frozenset[str]:
    """Tables that hold rows belonging to a particular warehouse.

    Read off the models, never written down. A hand-kept list is wrong the
    first time somebody adds a table — and it fails silently, because the new
    table is simply absent from the list and therefore readable by everyone,
    which is the failure nobody sees until it is in the news.

    Any column whose name ends in `warehouse_id` counts, which picks up
    `from_warehouse_id` and `to_warehouse_id` on transfers and
    `cross_dock_warehouse_id` on receipt lines as well as the plain column.

    Line tables that carry no warehouse of their own — `sales_order_lines` and
    the rest — are not in here, and that is not an oversight: any query that
    attributes a line to a branch has to join its parent, and the parent is in
    here. On their own they say what was moved, never where, and where is the
    thing being scoped.
    """
    return frozenset(
        name
        for name, table in Base.metadata.tables.items()
        if any(column.name.endswith("warehouse_id") for column in table.columns)
    )


def scope_guard(sql: str, allowed_warehouse_ids: list[int] | None) -> None:
    """Refuse a branch account any table that holds other branches' rows.

    `allowed_warehouse_ids` is what `app.core.deps.scoped_warehouse_ids`
    returns: None for managers and admins, who see the whole chain, and a list
    for everybody else. An empty list — a staff account with no branch set —
    is scoped to nothing and is refused like any other scoped caller, which is
    why this tests `is None` and not truthiness.

    Table names are matched as bare words rather than by working out which ones
    are really in the FROM clause. A query with a column aliased `stock_movements`
    is therefore refused for a scoped caller even though it touches nothing;
    that is the intended trade, and the alternative is a FROM-clause parser
    whose gaps are all in the other direction.
    """
    if allowed_warehouse_ids is None:
        return

    touched = sorted(scoped_tables() & set(_words(_readable(sql))))
    if touched:
        raise UnsafeQuery(
            f"your account is limited to one branch, and this question reads "
            f"{', '.join(touched)}, which hold every branch's rows. Ask "
            f"cannot narrow the query for you — adding a warehouse filter "
            f"would answer a different question from the one you asked, "
            f"without saying so. Chain-wide figures need a manager account; "
            f"your own branch's are on the Stock and Orders screens, which "
            f"are already scoped to it"
        )


# ----------------------------------------------------------------- credentials


@cache
def _unmistakable_user_columns() -> frozenset[str]:
    """Columns of `users` that no other table in the schema also has.

    This is what makes the credential check possible without a parser. Half the
    columns on `users` share their names with columns everywhere else —
    `created_at`, `warehouse_id`, `is_active`, `id` — so a bare `created_at` in
    a five-table join says nothing about whose it is, and refusing on the name
    alone would refuse most honest questions.

    `password_hash` and `last_login_at` belong to exactly one table in this
    database, so wherever they appear they are that table's, and they can be
    refused on sight at any depth, through any alias, in any subquery — even
    where the query never says the word `users`. The ambiguous ones are left to
    the alias check below, which needs the query to say which table it means.

    Computed, not listed, so that a column added to `users` tomorrow is covered
    the day it lands.
    """
    elsewhere = {
        column.name
        for name, table in Base.metadata.tables.items()
        if name != User.__tablename__
        for column in table.columns
    }
    return frozenset(
        column.name
        for column in User.__table__.columns
        if column.name not in elsewhere
        and column.name not in PERMITTED_USER_COLUMNS
    )


def _check_users_table(text: str) -> None:
    """Let a question join `users` for a person's name and nothing further."""
    aliases = {
        alias for alias in _USERS_ALIAS.findall(text)
        if alias not in _NOT_AN_ALIAS
    }
    handles = aliases | {User.__tablename__}

    # `SELECT *` against users is the trap, and it is a trap precisely because
    # it does not look like an attack — it is what anyone writes first, and it
    # returns the password hash without ever naming it. A star anywhere in a
    # query that mentions users is refused, including `p.*` on some other
    # table, because working out which table a star belongs to is the parsing
    # this module deliberately does not do.
    if _QUALIFIED_STAR.search(text) or _BARE_STAR.search(text):
        raise UnsafeQuery(
            f"a question that reads the users table has to name the columns it "
            f"wants — `*` would hand back the password hash along with them. "
            f"Available: {_PERMITTED_PHRASE}"
        )

    # The same leak wearing a different hat: `row_to_json(u)`, `to_jsonb(u)`
    # and `u::text` all serialise the entire row, hash included, without a
    # single column being named. Rather than keeping a list of the functions
    # that do this — which would miss the next one — the rule is that the table
    # and its aliases may only ever appear with a column attached, or in the
    # position that introduces the name in the first place.
    for match in _WORD.finditer(text):
        name = match.group(0)
        if name not in handles:
            continue
        if text[match.end():].lstrip().startswith("."):
            continue
        previous = _preceding_token(text, match.start())
        # The table's own name is what stands before an alias being bound, as
        # in `FROM users u`. Another *alias* standing there is not the same
        # thing and is not allowed: `SELECT u u FROM users u` is Postgres for
        # "the whole row, labelled u", which is the leak wearing a third hat.
        if previous in _BINDS_A_NAME or previous == User.__tablename__:
            continue
        if previous == ",":
            raise UnsafeQuery(
                f"`{name}` follows a comma, and a comma stands in front of a "
                f"table in `FROM receipts, users` as readily as in front of a "
                f"whole row in `SELECT id, users` — write JOIN, which says "
                f"which one is meant, and name the columns you want "
                f"({_PERMITTED_PHRASE})"
            )
        raise UnsafeQuery(
            f"`{name}` is being read as a whole row of the users table, which "
            f"carries the password hash — name the columns you want "
            f"({_PERMITTED_PHRASE})"
        )

    for chain in _DOTTED.findall(text):
        parts = [part.strip() for part in chain.split(".")]
        owner, column = parts[-2], parts[-1]
        if owner in handles and column not in PERMITTED_USER_COLUMNS:
            raise UnsafeQuery(
                f"`{owner}.{column}` is not readable — a question may name "
                f"only {_PERMITTED_PHRASE} of a person"
            )

    # An unqualified column can be attributed only when there is one table to
    # attribute it to — and then it can be attributed exactly: `SELECT is_active
    # FROM users` is about a user whether it says so or not.
    tables_named = set(_words(text)) & set(Base.metadata.tables)
    if tables_named == {User.__tablename__}:
        withheld = {c.name for c in User.__table__.columns} - PERMITTED_USER_COLUMNS
        for word in _words(text):
            if word in withheld:
                raise UnsafeQuery(
                    f"`{word}` is not readable — a question may name only "
                    f"{_PERMITTED_PHRASE} of a person"
                )


def credential_guard(sql: str) -> None:
    """Refuse any query that could return a credential.

    Two things are unreachable outright, at any depth and under any alias: the
    password hash, and the refresh-token table. There is no reporting question
    whose answer involves either, so there is no cost to refusing them and no
    version of "but this case is fine" to argue about.

    Everything else about `users` is handled by `_check_users_table`, which
    keeps the legitimate join — a name against a goods receipt — working.
    """
    text = _readable(sql)
    words = set(_words(text))

    if RefreshToken.__tablename__ in words:
        raise UnsafeQuery(
            f"`{RefreshToken.__tablename__}` holds session credentials and is "
            f"not readable through Ask by anyone, for any reason"
        )

    # Named off the model rather than typed out, so that renaming the column in
    # `app/models/identity.py` cannot leave this guard watching a column that
    # no longer exists while the one that replaced it goes unwatched.
    if User.password_hash.key in words:
        raise UnsafeQuery(
            f"`{User.password_hash.key}` is a credential — it is never "
            f"readable, by any account, through any question"
        )

    named = sorted(_unmistakable_user_columns() & words)
    if named:
        raise UnsafeQuery(
            f"`{named[0]}` belongs to the users table and is not readable — "
            f"a question may name only {_PERMITTED_PHRASE} of a person"
        )

    if User.__tablename__ in words:
        _check_users_table(text)


# ------------------------------------------------------------------ public API


def check_sql(
    sql: str,
    *,
    allowed_warehouse_ids: list[int] | None,
    cap: int = DEFAULT_ROW_CAP,
) -> str:
    """Every guard, in order, returning the query as it is allowed to run.

    Raises `UnsafeQuery` with the reason otherwise. The order is chosen for the
    message rather than for safety — all four have to pass — but a stacked
    statement makes every later finding noise, and someone reaching for a
    password hash should be told about that rather than about their branch.
    """
    _check_statement_shape(sql)
    credential_guard(sql)
    scope_guard(sql, allowed_warehouse_ids)
    return enforce_row_limit(sql, cap)
