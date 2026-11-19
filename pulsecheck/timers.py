"""The crew's own clock: "do this every minute / every hour", done by ONE worker.

The web app runs several worker threads (and maybe `flask worker` too). If
each of them started the canary every minute, it would run four times. So a
timer is a row (name, next_at), and claim() takes it with a compare-and-swap:

    UPDATE timers SET next_at = <later> WHERE name = 'canary' AND next_at = <what we read>

Only one worker's UPDATE matches; the others see rowcount 0 and skip it.
"""
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .models import Timer


def claim(s, name, now, every_seconds):
    """True for exactly one caller once `name` is due; it is then due again `every_seconds` later.
    The caller commits."""
    following = now + timedelta(seconds=every_seconds)
    due_at = s.scalar(select(Timer.next_at).where(Timer.name == name))
    if due_at is None:
        try:
            with s.begin_nested():  # two workers adding the first row at once: one wins
                s.add(Timer(name=name, next_at=following))
        except IntegrityError:
            return False
        return True
    if due_at > now:
        return False
    won = s.execute(update(Timer).where(Timer.name == name, Timer.next_at == due_at)
                    .values(next_at=following).execution_options(synchronize_session=False))
    return won.rowcount == 1


def next_at(s, name):
    return s.scalar(select(Timer.next_at).where(Timer.name == name))
