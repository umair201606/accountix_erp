"""What a user is told when another user's simultaneous change wins, and the
placeholder that stands in for a document number until the row has its id.

The races themselves run on Postgres in tests/concurrency."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy.exc import IntegrityError, OperationalError  # noqa: E402
from sqlalchemy.orm.exc import StaleDataError  # noqa: E402

from shared.concurrency import CONFLICT, DUPLICATE, GONE, conflict_message  # noqa: E402
from shared.doc_numbers import is_placeholder, placeholder  # noqa: E402


class _PgError(Exception):
    def __init__(self, code, text=""):
        super().__init__(text)
        self.pgcode = code


def _wrap(cls, code, text=""):
    return cls("stmt", {}, _PgError(code, text))


def test_deadlocks_and_lock_waits_read_as_another_users_change():
    assert conflict_message(_wrap(OperationalError, "40P01", "deadlock detected")) == CONFLICT
    assert conflict_message(_wrap(OperationalError, "55P03", "lock timeout")) == CONFLICT
    assert conflict_message(_wrap(OperationalError, "40001", "could not serialize")) == CONFLICT
    assert conflict_message(StaleDataError("row changed")) == CONFLICT


def test_sqlite_busy_reads_the_same():
    assert conflict_message(OperationalError("stmt", {}, Exception("database is locked"))) == CONFLICT


def test_unique_and_foreign_key_refusals_are_named():
    assert conflict_message(_wrap(IntegrityError, "23505", "duplicate key")) == DUPLICATE
    assert conflict_message(_wrap(IntegrityError, "23503", "violates foreign key")) == GONE


def test_real_errors_are_not_disguised_as_conflicts():
    # A NOT NULL violation or a lost connection is a bug or an outage, not
    # another user's change: it must reach the error handler as itself.
    assert conflict_message(_wrap(IntegrityError, "23502", "null value in column")) is None
    assert conflict_message(_wrap(OperationalError, "08006", "connection lost")) is None
    assert conflict_message(ValueError("nope")) is None


def test_placeholders_are_unique_and_recognisable():
    a, b = placeholder(), placeholder()
    assert a != b and is_placeholder(a) and is_placeholder(b)
    assert len(a) <= 50                      # fits the number columns
    assert not is_placeholder("SI-202610-0042") and not is_placeholder(None)
