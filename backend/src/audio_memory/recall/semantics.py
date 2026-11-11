"""Conservative, shared input semantics; no inferred identity or calendar facts."""

import re
from datetime import UTC, date, datetime

PRECISIONS = {'unknown', 'year', 'month', 'day', 'minute', 'second'}


def identity_status(value=None, *, matched=False):
    # A model saying "confirmed" is not a user/voice identity verification.
    return 'contextual' if matched else 'unresolved'


def anonymous_label(value):
    return bool(re.fullmatch(r'(?:说话人|讲话人|speaker)[\s_:#-]*[\w一二三四五六七八九十]+(?:\s*[（(][^）)]*[）)])?', value.strip(), re.I))


def time_value(value):
    if re.fullmatch(r'\d{4}', value):
        return date(int(value), 1, 1), 'year'
    if re.fullmatch(r'\d{4}-\d{2}', value):
        return date.fromisoformat(value + '-01'), 'month'
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return date.fromisoformat(value), 'day'
    if 'T' not in value:
        raise ValueError('invalid_valid_time')
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError('valid_time_timezone_required')
    shape = value.split('T')[1]
    return stamp.astimezone(UTC), 'second' if re.match(r'\d{2}:\d{2}:\d{2}', shape) else 'minute'


def validity(point, start, end, precision):
    if precision not in PRECISIONS:
        raise ValueError('invalid_valid_precision')
    if point is not None and (start is not None or end is not None):
        raise ValueError('valid_point_and_interval_exclusive')
    values = [time_value(v) for v in (point, start, end) if v is not None]
    if not values:
        if precision != 'unknown':
            raise ValueError('valid_precision_without_time')
        return precision
    shapes = {p for _, p in values}
    if len(shapes) != 1:
        raise ValueError('mixed_valid_precision')
    actual = shapes.pop()
    if precision not in {'unknown', actual}:
        raise ValueError('valid_precision_mismatch')
    if start is not None and end is not None and values[0][0] > values[1][0]:
        raise ValueError('invalid_valid_time')
    return actual


def task_metadata(task):
    kind = task.get('task_kind', 'explicit')
    intent = task.get('intent_strength', 'unspecified')
    if kind not in {'explicit', 'opportunity'}:
        raise ValueError('invalid_task_kind')
    if intent not in {'committed', 'tentative', 'conditional', 'unspecified'}:
        raise ValueError('invalid_task_intent')
    return dict(task_kind=kind, intent_strength=intent)


def overlaps_validity(point, start, end, first_day, last_day, timezone, *, precision=None):
    """Compare calendar precision as a span; never invent a known event date."""
    from calendar import monthrange
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(timezone)
    point, start, end, _ = interpret_validity(point, start, end, precision or "unknown")

    def bounds(value):
        parsed, precision = time_value(value)
        if precision in {'minute','second'}:
            return parsed, parsed, False
        lo = datetime.combine(parsed, time.min, zone)
        if precision == 'year':
            last = date(parsed.year,12,31)
        elif precision == 'month':
            last = date(parsed.year,parsed.month,monthrange(parsed.year,parsed.month)[1])
        else:
            last = parsed
        # datetime.max is only for the largest representable terminal date.
        hi = (datetime.combine(last + timedelta(days=1),time.min,zone)
              if last < date.max else datetime.max.replace(tzinfo=zone))
        return lo, hi, True

    query_start=datetime.combine(date.fromisoformat(str(first_day)),time.min,zone)
    last=date.fromisoformat(str(last_day))
    query_end=(datetime.combine(last+timedelta(days=1),time.min,zone)
               if last<date.max else datetime.max.replace(tzinfo=zone))
    try:
        if point:
            lo,hi,exclusive=bounds(point)
            return lo<query_end and (hi>query_start if exclusive else hi>=query_start)
        if not start and not end:
            return False
        lo=bounds(start)[0] if start else None
        hi=bounds(end) if end else None
        return (lo is None or lo<query_end) and (hi is None or (hi[1]>query_start if hi[2] else hi[1]>=query_start))
    except (ValueError, TypeError, OverflowError):
        # Legacy malformed values are not silently treated as valid matches.
        return False


def enum_domain(prop):
    values = prop.get('enum_values')
    if values is None:
        return None
    if prop.get('dtype') != 'enum' or not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
        raise ValueError('invalid_enum_domain')
    return values


def check_enum_definition(prop, texts):
    """A model's observed values are not an authoritative closed domain."""
    values = enum_domain(prop)
    if values is None:
        return
    if not prop.get('enum_evidence'):
        raise ValueError('enum_definition_evidence_required')
    text = '\n'.join(texts)
    if (prop['name'] not in text or any(value not in text for value in values)
        or not re.search(r'只能|仅限|取值范围|枚举值|可选值|allowed values|must be one of', text, re.I)):
        raise ValueError('enum_definition_not_in_evidence')


def interpret_validity(point, start, end, precision):
    """Read legacy encodings conservatively; never rewrite content-keyed facts."""
    def coarse(value):
        if value and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            # Old versions encoded coarse dates as YYYY-MM-DD while recording
            # their true granularity separately. Never promote them to day.
            return value[:4] if precision == 'year' else value[:7] if precision == 'month' else value
        return value
    point, start, end = [coarse(v) for v in (point, start, end)]
    if point is not None and point == start == end:
        start = end = None
    try:
        actual = validity(point, start, end, precision)
    except (ValueError, TypeError):
        return None, None, None, 'unknown'
    return point, start, end, actual


def interpreted_fact_time(row):
    result = dict(row)
    original = tuple(result.get(k) for k in ('valid_at', 'valid_from', 'valid_to', 'valid_precision'))
    interpreted = interpret_validity(*original)
    if original != interpreted:
        result.update(zip(('valid_at', 'valid_from', 'valid_to', 'valid_precision'), interpreted))
        result['validity_note'] = '历史时间字段按已记录精度兼容解释，数据库原值未改写；不能确定的时间不作依据。'
    return result
