"""Private append-only timings. No prompts, source text, secrets or decisions.

Disabled outside an explicit session; telemetry failures never change business
results. Parent wall time includes children; exclusive time excludes their union
so concurrent children cannot produce negative time or double-counted totals.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, UTC
from functools import wraps
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

_session = ContextVar('recall_performance_session', default=None)
_parent = ContextVar('recall_performance_parent', default=None)
logger = logging.getLogger(__name__)


@contextmanager
def timing_session(path, kind, identity):
    token = _session.set(dict(path=Path(path), operation_kind=kind, operation_id=identity))
    parent = _parent.set(None)
    try:
        yield
    finally:
        _parent.reset(parent)
        _session.reset(token)


def emit(name, **fields):
    session = _session.get()
    if session is None:
        return
    row = dict(at=datetime.now(UTC).isoformat(), operation_kind=session['operation_kind'],
               operation_id=session['operation_id'], stage=name, **fields)
    row.setdefault('parent_id', (_parent.get() or {}).get('id'))
    try:
        path = session['path']
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = (json.dumps(row, ensure_ascii=False, separators=(',', ':'))+'\n').encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        logger.warning('Recall performance telemetry unavailable', exc_info=False)


@contextmanager
def span(name, **fields):
    if _session.get() is None:
        yield
        return
    parent = _parent.get()
    own = dict(id=str(uuid4()), children=[])
    token = _parent.set(own)
    started = time.perf_counter()
    status, error_type = 'ok', None
    try:
        yield
    except BaseException as error:
        status, error_type = 'error', type(error).__name__
        raise
    finally:
        ended = time.perf_counter()
        intervals = sorted(own['children'])
        union, last = 0., started
        for a, b in intervals:
            union += max(0., b-max(a, last)); last = max(last, b)
        if parent is not None:
            parent['children'].append((started, ended))
        _parent.reset(token)
        emit(name, span_id=own['id'], parent_id=parent['id'] if parent else None,
             elapsed_ms=(ended-started)*1000,
             exclusive_ms=max(0., ended-started-union)*1000,
             status=status, error_type=error_type, **fields)


def timed_stage(name):
    def decorate(function):
        @wraps(function)
        async def timed(*args, **kwargs):
            with span(name):
                return await function(*args, **kwargs)
        return timed
    return decorate


def timed_build(function):
    @wraps(function)
    async def timed(self, build_id, *args, **kwargs):
        # Experiments can supply a separate timing session and database root.
        if _session.get() is not None:
            with span('build'):
                return await function(self, build_id, *args, **kwargs)
        path = getattr(self.database, 'path', None)
        if path is None:
            return await function(self, build_id, *args, **kwargs)
        with timing_session(Path(path).parent/'recall/performance/stages.jsonl', 'build', build_id):
            with span('build'):
                return await function(self, build_id, *args, **kwargs)
    return timed


def summarize(path):
    """Report stage distributions separately; nested totals are not additive."""
    import statistics
    stages, hits, incomplete = {}, {}, 0
    for line in Path(path).read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            incomplete += 1
            continue
        name = row['stage']
        if row.get('cached'):
            hits[name] = hits.get(name, 0)+1
        if 'elapsed_ms' in row:
            group = stages.setdefault(name, dict(samples=[], exclusive_ms=0., errors=0))
            group['samples'].append(row['elapsed_ms'])
            group['exclusive_ms'] += row['exclusive_ms']
            group['errors'] += row['status'] != 'ok'
    for group in stages.values():
        samples = sorted(group.pop('samples'))
        group.update(count=len(samples), inclusive_ms=sum(samples),
                     median_ms=statistics.median(samples),
                     p95_ms=samples[max(0, __import__('math').ceil(.95*len(samples))-1)])
    return dict(stages=stages, cache_hits=hits, incomplete_lines=incomplete,
                note='Nested inclusive times overlap; do not sum as elapsed wall time.')
