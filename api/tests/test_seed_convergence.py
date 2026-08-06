"""What the seed must do to a database it did not create (app/seed/bootstrap.py).

There are two kinds of work in the seed and the difference is not cosmetic.

`seed()` is a fixture: products, branches, demo users, two invented GST
registrations. It runs once, on an empty database, and must never touch one it
did not build — a made-up registration number printed on a real tax invoice is
worse than a missing one.

`converge()` is the other kind. Permissions, the roles that hold them and the
feature flags that expose them are not data the business owns; they are what
the running code *is*, kept in tables because the authorisation check reads
them from tables. They have to be true of every database this code runs
against, including one seeded two years ago.

`main()` returns early on any database that already holds a user, so which side
of that return a piece of work sits on decides whether it ever reaches a
running server. `sync_permissions` and `sync_roles` sat on the wrong side for
months: the deployed box had whatever authorisation rows it was provisioned
with, and the next PR to add a permission would have shipped an endpoint that
refused everybody, with no clue beyond a role quietly missing a grant.

So that is what these pin down — not the contents of either category, but the
line between them. No database and no server; `main()` is driven directly with
a stub session.
"""

from __future__ import annotations

import pytest

import app.seed.bootstrap as bootstrap


class FakeSession:
    """Enough of a Session for `main()`, and a note of what it was asked.

    `seeded` decides the one branch that matters: `main()` reads a user id to
    tell an established database from an empty one.
    """

    def __init__(self, *, seeded: bool):
        self._seeded = seeded
        self.commits = 0

    def scalar(self, _stmt):
        return 1 if self._seeded else None

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """Record which halves ran, and stop either from reaching a database."""
    ran: list[str] = []

    def converge(_db):
        ran.append("converge")
        return "permissions: 0   roles: 0   flags: 0"

    def seed(_db):
        ran.append("seed")

    monkeypatch.setattr(bootstrap, "converge", converge)
    monkeypatch.setattr(bootstrap, "seed", seed)
    # Reads the demo accounts to compare hashes; there are none in a stub.
    monkeypatch.setattr(bootstrap, "_warn_if_password_drifted", lambda _db: None)
    monkeypatch.setattr(bootstrap.sys, "argv", ["bootstrap"])
    return ran


def _run(monkeypatch, *, seeded: bool) -> FakeSession:
    session = FakeSession(seeded=seeded)
    monkeypatch.setattr(bootstrap, "SessionLocal", lambda: session)
    bootstrap.main()
    return session


def test_an_established_database_still_converges(monkeypatch, calls):
    """The regression, stated plainly.

    A server that has been running since the day it was provisioned takes the
    early return on every restart. If the convergent work is behind that
    return, a permission added in any later release never reaches the box, and
    the endpoint guarding it answers 403 to everybody who is in fact allowed.
    """
    _run(monkeypatch, seeded=True)

    assert "converge" in calls


def test_an_established_database_is_not_re_seeded(monkeypatch, calls):
    """The other half of the same line.

    Convergence reaching a live database is only safe because the fixture does
    not follow it there. `seed()` collides on unique constraints and doubles
    every opening balance, and its GST registrations are invented — running it
    against a real installation would write a fictional tax number onto rows
    that go on invoices.
    """
    _run(monkeypatch, seeded=True)

    assert calls == ["converge"]


def test_an_empty_database_gets_both(monkeypatch, calls):
    """A fresh clone or a dropped volume: converge first, because `seed()`
    attaches a role to each demo user and the roles have to exist by then."""
    _run(monkeypatch, seeded=False)

    assert calls == ["converge", "seed"]


def test_convergence_is_committed_before_the_early_return(monkeypatch, calls):
    """Rolled-back convergence is no convergence.

    `main()` returns straight after the skip message on an established
    database, so anything not committed by that point is discarded when the
    session closes — the work would appear to run on every restart and land
    never.
    """
    session = _run(monkeypatch, seeded=True)

    assert session.commits >= 1
