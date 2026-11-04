#!/usr/bin/env python3
"""One-shot progress reminder. Run wait as a host-managed asynchronous process."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time

FORMAT = 'batch-flow-progress-timer/v1'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


@contextlib.contextmanager
def locked(ticket):
    ticket.parent.mkdir(parents=True, exist_ok=True)
    with ticket.with_name(ticket.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save(ticket, record):
    fd, name = tempfile.mkstemp(prefix='.timer-', dir=str(ticket.parent))
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(record, out, ensure_ascii=False)
            out.write('\n')
        os.replace(name, ticket)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(ticket):
    record = json.loads(ticket.read_text())
    if record.get('format') != FORMAT or record.get('status') not in {'running', 'due', 'cancelled'}:
        raise ValueError('not a progress timer ticket')
    return record


def emit(event, record, ticket):
    print(json.dumps(dict(event=event, ticket=str(ticket), **record), ensure_ascii=False), flush=True)


def wait(ticket, task, goal, seconds, handle=None):
    # A new ticket for each interval prevents stale cancellation and duplicate timers.
    with locked(ticket):
        if ticket.exists():
            raise ValueError('ticket already exists; use a new ticket for each interval')
        record = dict(format=FORMAT, status='running', task=task, goal=goal,
                      handle=handle, seconds=seconds, started_at=now())
        save(ticket, record)
    emit('TIMER_STARTED', record, ticket)
    started = time.monotonic()
    deadline = started + seconds
    while True:
        with locked(ticket):
            record = read(ticket)
            if record['status'] == 'cancelled':
                emit('TIMER_CANCELLED', record, ticket)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                record.update(status='due', ended_at=now(), elapsed_seconds=time.monotonic()-started)
                save(ticket, record)
                emit('PROGRESS_DUE', record, ticket)
                return
        time.sleep(min(remaining, 0.2))


def cancel(ticket):
    with locked(ticket):
        record = read(ticket)
        if record['status'] == 'running':
            record.update(status='cancelled', ended_at=now())
            save(ticket, record)
        emit('TIMER_CANCELLED' if record['status'] == 'cancelled' else 'TIMER_ALREADY_DUE', record, ticket)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    waiting = sub.add_parser('wait', help='foreground one-shot timer; dispatch asynchronously through the host')
    waiting.add_argument('--ticket', type=Path, required=True)
    waiting.add_argument('--task', required=True)
    waiting.add_argument('--goal', required=True)
    waiting.add_argument('--handle', help='existing agent/session/process handle, for correlation only')
    waiting.add_argument('--seconds', type=float, default=600)
    stopping = sub.add_parser('cancel', help='cancel this timer without signalling the task process')
    stopping.add_argument('--ticket', type=Path, required=True)
    args = parser.parse_args()
    try:
        ticket = args.ticket.absolute()
        if args.command == 'wait':
            if not math.isfinite(args.seconds) or args.seconds <= 0:
                raise ValueError('seconds must be finite and greater than zero')
            if not args.task.strip() or not args.goal.strip():
                raise ValueError('task and goal must be non-empty')
            wait(ticket, args.task, args.goal, args.seconds, args.handle)
        else:
            cancel(ticket)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps(dict(event='TIMER_ERROR', error=str(error)), ensure_ascii=False), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
