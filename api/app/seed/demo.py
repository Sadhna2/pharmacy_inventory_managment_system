"""The whole demonstrable dataset, in one command, anywhere.

    python -m app.seed.demo              # build it, or do nothing if it exists
    python -m app.seed.demo --rebuild    # tear the generated part down first

This exists so that "what the app looks like" is not a property of which
machine you started it on.

There are three seeds and they have to run in order — the catalogue, then two
years of trading on top of it, then the states that trading never produces. For
a while only the first of them ran in the container, so the deployed site had
twelve products, no sales, and every Analysis screen correctly reporting that
it had nothing to work with, while a developer running all three by hand had a
fully populated system. Both were "the app", and they did not resemble each
other.

Each step is guarded, so this is safe as a container start-up command: the
first boot builds the dataset over a few minutes, every later one costs three
COUNT queries. That guard is the reason this can be the same command in
docker-compose and in the README instead of two lists of steps that drift.

The data is deterministic — the generator takes a fixed RNG seed — so the same
commit produces the same history everywhere. It is anchored to *today*, though,
so a box seeded last week holds batches a week closer to expiry than one seeded
this morning. Use --rebuild when that matters.
"""

import argparse
import subprocess
import sys

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.identity import User
from app.seed import history, showcase


def _run(*args: str) -> None:
    """Run a seed module in its own process.

    A subprocess rather than an import-and-call: each of these owns a session,
    commits on its own schedule, and `history` in particular holds a large
    working set it is better to hand back to the OS at exit. Failure is fatal
    and deliberately so — a half-built dataset is worse than an empty one,
    because it looks finished.
    """
    printable = " ".join(args)
    print(f"\n$ python -m {printable}", flush=True)
    result = subprocess.run([sys.executable, "-m", *args], check=False)
    if result.returncode != 0:
        raise SystemExit(f"`{printable}` failed with exit code {result.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=730, help="days of history")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="replace the generated history and showcase rather than keeping them",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        seeded = db.scalar(select(User.id).limit(1)) is not None
        rows = history.generated_rows(db)
        extra = showcase.applied_rows(db)

    print(
        "Current state: "
        f"users={'yes' if seeded else 'no'}, "
        f"history={rows:,} rows, showcase={'applied' if extra else 'no'}"
    )

    # Idempotent on its own — it checks for an existing user and returns.
    _run("app.seed.bootstrap")

    if args.rebuild:
        _run("app.seed.history", "--days", str(args.days), "--reset")
        _run("app.seed.showcase")
    else:
        _run("app.seed.history", "--days", str(args.days), "--if-empty")
        _run("app.seed.showcase", "--if-empty")

    print("\nDataset ready.")


if __name__ == "__main__":
    main()
