"""SEED_PASSWORD and the demo accounts (app/seed/bootstrap.py).

The seed short-circuits on an already-populated database — it has to, since
re-running it would collide on unique constraints and double every opening
balance. The cost of that short-circuit is that changing SEED_PASSWORD in
`.env` and restarting does nothing at all: the stored hash keeps whatever the
*first* run used, and you meet a login that rejects the only password you were
ever told to use.

These tests run with no database and no server: they drive the two functions
directly with a stub session.
"""

from __future__ import annotations

import app.seed.bootstrap as bootstrap
from app.core.security import hash_password, verify_password


class FakeUser:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password_hash = hash_password(password)


class FakeSession:
    """Resolves `select(User).where(User.email == ...)` by remembered order.

    `reset_demo_passwords` asks for one account at a time, in USER_DEFS order,
    so returning them in that order is enough and keeps the stub honest about
    what the function actually does.
    """

    def __init__(self, users: list[FakeUser | None]):
        self._queue = list(users)
        self.flushed = False

    def scalar(self, _stmt):
        return self._queue.pop(0) if self._queue else None

    def flush(self):
        self.flushed = True


def _accounts(password: str) -> list[FakeUser]:
    return [FakeUser(email, password) for email, *_ in bootstrap.USER_DEFS]


def test_reset_rehashes_every_account_whose_password_drifted(monkeypatch):
    monkeypatch.setattr(bootstrap, "DEV_PASSWORD", "NewOne@2026")
    users = _accounts("TheOldOne@2026")
    session = FakeSession(list(users))

    changed = bootstrap.reset_demo_passwords(session)

    assert changed == len(bootstrap.USER_DEFS)
    for user in users:
        assert verify_password("NewOne@2026", user.password_hash)
        assert not verify_password("TheOldOne@2026", user.password_hash)


def test_reset_is_idempotent(monkeypatch):
    """Running it twice must not report work it did not do."""
    monkeypatch.setattr(bootstrap, "DEV_PASSWORD", "Settled@2026")
    users = _accounts("Settled@2026")
    before = [u.password_hash for u in users]

    changed = bootstrap.reset_demo_passwords(FakeSession(list(users)))

    assert changed == 0
    assert [u.password_hash for u in users] == before


def test_reset_tolerates_a_missing_account(monkeypatch):
    """A half-populated database must not crash the reset."""
    monkeypatch.setattr(bootstrap, "DEV_PASSWORD", "NewOne@2026")
    present = FakeUser(bootstrap.USER_DEFS[0][0], "TheOldOne@2026")
    session = FakeSession([present, None, None, None])

    assert bootstrap.reset_demo_passwords(session) == 1
    assert verify_password("NewOne@2026", present.password_hash)


def test_a_drifted_password_is_reported(monkeypatch, capsys):
    """The whole point: the mismatch must be said out loud.

    Without this the seed prints "already seeded — skipping", which is true and
    tells you nothing about the password you just changed.
    """
    monkeypatch.setenv("SEED_PASSWORD", "NewOne@2026")
    monkeypatch.setattr(bootstrap, "DEV_PASSWORD", "NewOne@2026")
    session = FakeSession([FakeUser(bootstrap.USER_DEFS[0][0], "TheOldOne@2026")])

    bootstrap._warn_if_password_drifted(session)

    out = capsys.readouterr().out
    assert "SEED_PASSWORD does not match" in out
    assert "--reset-passwords" in out


def test_a_matching_password_is_not_reported(monkeypatch, capsys):
    monkeypatch.setenv("SEED_PASSWORD", "Settled@2026")
    monkeypatch.setattr(bootstrap, "DEV_PASSWORD", "Settled@2026")
    session = FakeSession([FakeUser(bootstrap.USER_DEFS[0][0], "Settled@2026")])

    bootstrap._warn_if_password_drifted(session)

    assert capsys.readouterr().out == ""


def test_nothing_is_reported_when_no_password_was_asked_for(monkeypatch, capsys):
    """An unset SEED_PASSWORD cannot have drifted from anything.

    Every `docker compose up` without a `.env` takes this path, so a warning
    here would cry wolf on the most common run of all.
    """
    monkeypatch.delenv("SEED_PASSWORD", raising=False)
    monkeypatch.setattr(bootstrap, "DEV_PASSWORD", bootstrap._DEFAULT_DEV_PASSWORD)
    session = FakeSession([FakeUser(bootstrap.USER_DEFS[0][0], "SomethingElse@1")])

    bootstrap._warn_if_password_drifted(session)

    assert capsys.readouterr().out == ""
