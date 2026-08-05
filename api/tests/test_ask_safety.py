"""The guard that decides whether model-written SQL runs (app/ai/ask/safety.py).

Everything else in Ask can be wrong and produce a wrong number on a screen. If
this file is wrong the result is another branch's stock ledger, or a column of
password hashes, so the tests are written the way the attack would be: each one
is a specific route through, and each asserts the specific refusal rather than
merely that something was raised. A test that accepts any exception passes just
as happily when the query was refused for the wrong reason — and goes on
passing after the rule it was meant to protect has been deleted.

The quiet half is here for the same reason it is in `test_intake_validate.py`.
A guard that refuses everything is trivially safe and completely useless, and
the way it dies is that someone switches it off. So the honest questions are
tested too: a name joined off a goods receipt, a product list for a branch
account, the word DROP sitting harmlessly inside a string literal.
"""

import pytest

from app.ai.ask.safety import (
    DEFAULT_ROW_CAP,
    UnsafeQuery,
    check_sql,
    enforce_row_limit,
    is_single_select,
    scoped_tables,
)
from app.db.base import Base

#: A manager or administrator: `scoped_warehouse_ids` returns None for them and
#: they may read the whole chain.
CHAIN: list[int] | None = None
#: Pharmacy staff pinned to branch 2, which is the account every scoping test
#: below is really about.
BRANCH = [2]


def refuses(sql: str, *, scope: list[int] | None = CHAIN) -> str:
    """The refusal `sql` earns, or a failing test saying it was let through."""
    with pytest.raises(UnsafeQuery) as caught:
        check_sql(sql, allowed_warehouse_ids=scope)
    return str(caught.value)


def permits(sql: str, *, scope: list[int] | None = CHAIN) -> str:
    """The query as it would reach the database, capped and wrapped."""
    return check_sql(sql, allowed_warehouse_ids=scope)


# ------------------------------------------------------------ exactly one SELECT


def test_an_ordinary_question_is_allowed_through_unchanged():
    sql = "SELECT sku, name FROM products WHERE is_active ORDER BY name"
    assert sql in permits(sql)


def test_a_leading_cte_is_still_a_single_select():
    sql = (
        "WITH sold AS (SELECT product_id, qty_shipped FROM sales_order_lines) "
        "SELECT product_id, sum(qty_shipped) FROM sold GROUP BY product_id"
    )
    assert is_single_select(sql)
    assert sql in permits(sql)


def test_is_single_select_answers_the_question_its_name_asks():
    assert is_single_select("SELECT 1")
    assert not is_single_select("SELECT 1; SELECT 2")
    assert not is_single_select("DROP TABLE lots")


def test_a_second_statement_is_refused():
    assert "exactly one SELECT" in refuses("SELECT id FROM products; DELETE FROM lots")


def test_a_block_comment_cannot_smuggle_a_second_statement():
    """The example the strip-comments-first rule exists for.

    Anything checking the first word of the raw string sees SELECT and waves
    this straight through, and the DROP is sitting behind a comment where a
    keyword scan of the unstripped text is as likely to find `x` as to find it.
    """
    assert "exactly one SELECT" in refuses("SELECT 1; /*x*/ DROP TABLE lots")


def test_a_dash_comment_cannot_smuggle_a_second_statement():
    assert "exactly one SELECT" in refuses(
        "SELECT id FROM products -- nothing to see\n; DROP TABLE lots"
    )


def test_a_comment_between_the_letters_of_a_keyword_is_not_that_keyword():
    """Postgres reads `DR/**/OP` as two tokens and a syntax error, so this
    module has to as well — a comment collapses to a space, never to nothing.

    Deleting it instead would manufacture a DROP the server would never see.
    That direction is safe but it is not free: a guard that refuses queries for
    keywords they do not contain is a guard people learn to route around.
    """
    assert "`dr`" in refuses("DR/**/OP TABLE lots")


def test_case_folding_closes_the_first_thing_anyone_tries():
    assert "SeLeCt" in permits("SeLeCt 1 AS answer")
    assert "`update`" in refuses("SELECT n FROM (UpDaTe lots SET id = 1) n")


def test_a_cte_that_writes_is_refused_although_the_statement_opens_with_with():
    """A data-modifying CTE is why the keyword scan covers the whole query and
    not its first word. This one opens with WITH, closes with a SELECT, and
    inserts a row in between."""
    assert "`insert`" in refuses(
        "WITH new AS (INSERT INTO lots (product_id, lot_code) "
        "VALUES (1, 'X') RETURNING id) SELECT id FROM new"
    )


def test_a_write_buried_in_a_subquery_is_refused():
    assert "`delete`" in refuses(
        "SELECT id FROM products WHERE id IN "
        "(SELECT id FROM (DELETE FROM lots RETURNING id) gone)"
    )


@pytest.mark.parametrize("statement", [
    "INSERT INTO lots (product_id) VALUES (1)",
    "UPDATE products SET name = 'x'",
    "DELETE FROM lots",
    "TRUNCATE lots",
    "DROP TABLE lots",
    "ALTER TABLE products ADD COLUMN sneaky int",
    "CREATE TABLE spare (id int)",
    "GRANT SELECT ON products TO public",
    "COPY products TO '/tmp/out.csv'",
    "SET statement_timeout = 0",
    "RESET ALL",
])
def test_nothing_that_writes_or_reconfigures_counts_as_a_question(statement):
    assert "only run a SELECT" in refuses(statement)


def test_select_into_is_a_write_however_it_looks():
    """`SELECT ... INTO t` creates a table. It opens with SELECT, so the only
    thing standing between it and a new table is the word list."""
    assert "`into`" in refuses("SELECT sku INTO stolen FROM products")


def test_moving_a_sequence_is_a_write_too():
    assert "`setval`" in refuses("SELECT setval('document_sequences_id_seq', 1)")


@pytest.mark.parametrize("call", [
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT dblink('host=elsewhere', 'SELECT 1')",
])
def test_nothing_reaches_past_this_application_into_the_server(call):
    assert "the database server itself" in refuses(call)


def test_pg_sleep_is_refused_because_it_ties_up_a_connection():
    """Not a leak — a denial of service, and one that costs the model nothing
    to write. The pool is small and a question that never returns holds a
    connection out of it for as long as it likes."""
    assert "`pg_sleep`" in refuses("SELECT pg_sleep(600)")


def test_the_servers_own_password_hashes_are_out_of_reach():
    """`pg_shadow` and `pg_authid` hold Postgres's own role passwords. They are
    caught by the same prefix rule as `pg_sleep` rather than by a catalogue of
    names, because a catalogue of names is the part that goes stale."""
    assert "`pg_shadow`" in refuses("SELECT * FROM pg_shadow")


# --------------------------------------------------------- what must stay quiet


def test_a_forbidden_word_inside_a_comment_is_not_a_refusal():
    sql = "SELECT sku FROM products /* the DROP column went in v2 */"
    assert sql in permits(sql)


def test_a_forbidden_word_inside_a_string_literal_is_not_a_refusal():
    sql = "SELECT id FROM products WHERE name = 'DROP TABLE lots'"
    assert sql in permits(sql)


def test_a_semicolon_inside_a_string_is_not_a_second_statement():
    sql = "SELECT id FROM products WHERE description = 'take one; then rest'"
    assert sql in permits(sql)


def test_an_apostrophe_inside_a_value_is_data_like_any_other():
    """Product names have apostrophes in them, written `''` as SQL requires. A
    scanner that lost its place on one would spend the rest of the query
    treating code as text and text as code, and there is no telling which way
    round that lands."""
    sql = (
        "SELECT id FROM products "
        "WHERE name = 'one; then rest — children''s syrup' AND id > 0"
    )
    assert sql in permits(sql)


def test_block_comments_nest_the_way_postgres_nests_them():
    """Postgres nests them, so everything after the inner `*/` here is still
    comment. Reading it any other way puts this module and the server in
    disagreement about where the code is, and every disagreement of that kind
    is somewhere to hide something."""
    sql = "SELECT 1 /* outer /* inner */ DROP TABLE lots */"
    assert sql in permits(sql)


def test_an_unterminated_quote_is_refused_rather_than_guessed_at():
    """The one input where this scanner and the server are certain to be
    reading different text, which makes every check below it meaningless."""
    assert "unterminated" in refuses("SELECT name FROM products WHERE name = 'oops")


def test_a_quoted_identifier_cannot_open_a_comment():
    """A double-quoted identifier is consumed whole rather than re-scanned. Read
    the other way, `"a--"` opens a line comment that swallows the rest of the
    select list — and the rest of the select list is where the columns are."""
    assert "`password_hash`" in refuses('SELECT "a--", password_hash FROM users')


# ------------------------------------------------------------------- the row cap


def test_the_cap_is_applied_to_a_query_that_carries_no_limit():
    capped = enforce_row_limit("SELECT sku FROM products", 25)
    assert "SELECT sku FROM products" in capped
    assert capped.endswith("LIMIT 25")


def test_a_query_with_its_own_huge_limit_still_cannot_exceed_the_cap():
    """The case the wrapper exists for.

    Shrinking the query's own LIMIT means first deciding which LIMIT it is —
    the outer one, the one in the CTE, the one in the subquery in the WHERE
    clause — and being wrong about that once returns five million rows.
    """
    capped = enforce_row_limit("SELECT sku FROM products LIMIT 5000000", 25)
    assert capped.startswith("SELECT * FROM (")
    assert capped.endswith("LIMIT 25")
    # The query's own limit survives inside the subquery, where it can only
    # ever return fewer rows than the cap and never more.
    assert "LIMIT 5000000\n)" in capped


def test_a_limit_inside_a_cte_cannot_escape_the_cap_either():
    capped = enforce_row_limit(
        "WITH big AS (SELECT sku FROM products LIMIT 900000) "
        "SELECT sku FROM big",
        10,
    )
    assert capped.endswith("LIMIT 10")


def test_a_trailing_comment_cannot_comment_out_the_cap():
    """Without the newline the wrapper puts before its closing bracket, a query
    ending in a dash comment swallows `) AS ask_result LIMIT 25` along with it
    and runs uncapped."""
    capped = enforce_row_limit("SELECT sku FROM products -- all of them", 25)
    assert capped.splitlines()[-1] == ") AS ask_result LIMIT 25"


def test_a_trailing_semicolon_is_removed_before_wrapping():
    capped = enforce_row_limit("SELECT 1;  ", 25)
    assert ";" not in capped
    assert capped.endswith("LIMIT 25")


def test_a_cap_below_one_is_refused_rather_than_written_into_the_sql():
    with pytest.raises(UnsafeQuery) as caught:
        enforce_row_limit("SELECT 1", 0)
    assert "would return nothing" in str(caught.value)


def test_check_sql_returns_the_query_that_will_actually_run():
    ran = permits("SELECT sku FROM products")
    assert ran.endswith(f"LIMIT {DEFAULT_ROW_CAP}")


# --------------------------------------------------------------- branch scoping


def test_the_scoped_tables_are_read_off_the_models_and_not_written_down():
    """A hand-kept list is wrong the first time somebody adds a table, and it is
    wrong silently: the new table is simply absent from it, so everyone can
    read it. This asserts the derivation rather than the answer."""
    for name in scoped_tables():
        columns = {column.name for column in Base.metadata.tables[name].columns}
        assert any(c.endswith("warehouse_id") for c in columns), name
    assert "stock_balances" in scoped_tables()
    # from_warehouse_id and to_warehouse_id count as much as the plain column.
    assert "stock_transfers" in scoped_tables()
    assert "products" not in scoped_tables()


def test_a_manager_may_read_across_the_whole_chain():
    sql = (
        "SELECT warehouse_id, sum(qty_on_hand) FROM stock_balances "
        "GROUP BY warehouse_id"
    )
    assert sql in permits(sql, scope=CHAIN)


def test_a_branch_account_cannot_read_another_branchs_stock():
    """The data-leak path. Every warehouse's rows live in one table, the
    scoping that keeps them apart lives in `app/core/deps.py`, and raw SQL
    never goes anywhere near it."""
    refusal = refuses(
        "SELECT qty_on_hand FROM stock_balances WHERE warehouse_id = 7",
        scope=BRANCH,
    )
    assert "stock_balances" in refusal
    assert "every branch's rows" in refusal


def test_a_branch_account_is_refused_even_when_it_asks_for_its_own_branch():
    """Deliberate, and the most likely thing to be "fixed" by someone who has
    not read the guard.

    Honouring `warehouse_id = 2` means proving that the filter really does
    constrain the query — through the joins, the unions, the subqueries and the
    aggregates — and that proof is most of a query planner. Refusing costs the
    asker a rephrase; getting the proof 95% right hands them a wrong branch's
    numbers with no sign that anything happened.
    """
    assert "stock_balances" in refuses(
        "SELECT qty_on_hand FROM stock_balances WHERE warehouse_id = 2",
        scope=BRANCH,
    )


def test_a_scoped_table_hidden_in_a_cte_is_still_seen():
    assert "stock_movements" in refuses(
        "WITH moved AS (SELECT product_id, quantity FROM stock_movements) "
        "SELECT product_id, sum(quantity) FROM moved GROUP BY product_id",
        scope=BRANCH,
    )


def test_a_scoped_table_reached_only_through_a_join_is_still_seen():
    assert "goods_receipts" in refuses(
        "SELECT p.name, r.received_at FROM products p "
        "JOIN goods_receipt_lines l ON l.product_id = p.id "
        "JOIN goods_receipts r ON r.id = l.goods_receipt_id",
        scope=BRANCH,
    )


def test_master_data_stays_answerable_for_a_branch_account():
    """The guard has to leave something standing or the feature is theatre.
    Products, categories and suppliers belong to the chain rather than to a
    branch, so questions that only touch them are answered as usual."""
    sql = (
        "SELECT c.name, count(*) FROM products p "
        "JOIN categories c ON c.id = p.category_id GROUP BY c.name"
    )
    assert sql in permits(sql, scope=BRANCH)


def test_an_account_with_no_branch_at_all_is_scoped_rather_than_unscoped():
    """`scoped_warehouse_ids` returns [] for a staff account with no warehouse
    set, and an empty list is falsy. Testing it for truthiness rather than for
    None would hand the least-configured account in the system the widest
    access there is."""
    assert "stock_balances" in refuses("SELECT id FROM stock_balances", scope=[])


# ------------------------------------------------------------------ credentials


def test_the_password_hash_is_refused_when_it_is_named():
    refusal = refuses("SELECT email, password_hash FROM users")
    assert "password_hash" in refusal
    assert "credential" in refusal


def test_the_password_hash_is_refused_when_it_is_not_named():
    """`SELECT *` is the trap, and it is a trap because it does not look like
    one: it is what anyone writes first, and it returns the hash without ever
    mentioning the column it leaks."""
    refusal = refuses("SELECT * FROM users")
    assert "`*`" in refusal
    assert "password hash" in refusal


def test_a_star_on_a_join_that_includes_users_is_refused_too():
    assert "`*`" in refuses(
        "SELECT u.* FROM users u JOIN goods_receipts g ON g.received_by = u.id"
    )


def test_a_star_with_no_space_in_front_of_it_is_still_a_star():
    """`select*from users` is valid Postgres and reads as nothing at all to a
    pattern that expects whitespace after the keyword."""
    assert "`*`" in refuses("select*from users")


def test_a_comma_join_onto_users_is_refused_rather_than_guessed_at():
    """A comma stands in front of the table name in `FROM products p, users u`,
    which is a join — and in `SELECT id, users`, which is the whole row. Telling
    those two apart is parsing, so both are refused and the message asks for an
    explicit JOIN, which this layer can read."""
    assert "write JOIN" in refuses("SELECT p.name FROM products p, users u")


@pytest.mark.parametrize("whole_row", [
    "SELECT row_to_json(u) FROM users u",
    "SELECT to_jsonb(u) FROM users u",
    "SELECT json_agg(u) FROM users u",
    "SELECT u::text FROM users u",
])
def test_a_users_row_may_not_be_handed_to_a_function_whole(whole_row):
    """Each of these returns the password hash without naming a column, and
    there are more of them than anyone can keep a list of — hence the rule that
    the table and its aliases may appear only with a column attached."""
    assert "whole row" in refuses(whole_row)


def test_selecting_the_table_name_itself_is_selecting_the_row():
    """`SELECT users FROM users` is Postgres for the composite row, hash and
    all, and it never uses a star or names a column."""
    assert "whole row" in refuses("SELECT users FROM users")


def test_relabelling_an_alias_does_not_launder_the_row():
    """`SELECT u u FROM users u` is `SELECT u AS u` — the whole row wearing a
    label rather than a column reference, which is the shape a fix for the
    previous case would most plausibly be written to allow."""
    assert "whole row" in refuses("SELECT u u FROM users u")


def test_a_quoted_column_name_does_not_hide_the_hash():
    assert "password_hash" in refuses('SELECT "password_hash" FROM users')


def test_the_hash_is_caught_through_an_alias_and_a_subquery():
    assert "password_hash" in refuses(
        "SELECT x.secret FROM (SELECT u.password_hash AS secret FROM users u) x"
    )


def test_the_refresh_token_table_is_unreadable():
    refusal = refuses("SELECT token_hash FROM refresh_tokens")
    assert "refresh_tokens" in refusal
    assert "credentials" in refusal


def test_joining_users_for_a_name_still_works():
    """The check that matters most is this one. Who received a delivery is
    ordinary reporting, it is answered by joining users for a name, and a
    credential guard that breaks it will be turned off within the week."""
    sql = (
        "SELECT u.full_name, count(*) AS receipts "
        "FROM goods_receipts g JOIN users u ON u.id = g.received_by "
        "GROUP BY u.full_name"
    )
    assert sql in permits(sql)


@pytest.mark.parametrize("column", ["is_active", "role_id", "warehouse_id"])
def test_a_users_column_outside_the_permitted_three_is_refused(column):
    assert "may name only" in refuses(f"SELECT u.{column} FROM users u")


def test_a_column_that_only_users_has_is_refused_without_being_qualified():
    """`last_login_at` is on `users` and on no other table in the schema, so a
    bare mention of it can be attributed on sight — in a multi-table query,
    with nothing saying which table it came from. Nothing else in this guard
    catches this one: the alias rule needs the query to name an owner, and the
    unqualified rule needs there to be a single table to attribute it to."""
    assert "may name only" in refuses(
        "SELECT last_login_at FROM goods_receipts g "
        "JOIN users u ON u.id = g.received_by"
    )


def test_a_shared_column_name_is_not_mistaken_for_the_users_one():
    """The other half of that. `created_at` is on nearly every table; refusing
    it because `users` also has one would refuse most honest questions, and a
    guard nobody can work with is a guard that gets removed."""
    sql = (
        "SELECT g.created_at, u.full_name FROM goods_receipts g "
        "JOIN users u ON u.id = g.received_by"
    )
    assert sql in permits(sql)


def test_an_unqualified_column_is_attributed_when_users_is_the_only_table():
    assert "may name only" in refuses("SELECT is_active FROM users")


def test_counting_users_needs_no_column_and_is_allowed():
    sql = "SELECT count(*) FROM users"
    assert sql in permits(sql)


def test_a_scoped_account_reaching_for_the_hash_is_told_about_the_hash():
    """Both guards refuse this one. The credential guard runs first because it
    is the more serious thing to have been asked, and because "your account is
    limited to one branch" would imply a manager could have it."""
    assert "credential" in refuses("SELECT password_hash FROM users", scope=BRANCH)
