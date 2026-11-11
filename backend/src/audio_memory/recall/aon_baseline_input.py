"""Prepare one imported source for the V9-4 → SPV-F4 build."""
import json

from .tiered import Corpus


def segmentation_input(snapshot):
    """Retain stable source UIDs and assign speaker aliases within this recording."""
    aliases = {}
    segments = []
    for row in snapshot['segments']:
        speaker = row['speaker_local_id']
        if speaker is not None and speaker not in aliases:
            aliases[speaker] = f'S{len(aliases)}'
        segments.append({k: row[k] for k in ('ordinal', 'uid', 'text', 'start_ms', 'end_ms')} |
                        {'speaker_local_id': aliases.get(speaker, '未知')})
    header = dict(after=[], before=[], previous=None, previous_tail=[],
                  recorded_date=snapshot['recorded_date'], recorded_start_at=snapshot['recorded_start_at'],
                  source_revision_id=snapshot['source_revision_id'],
                  speaker_registry={v:k for k,v in aliases.items()})
    lines = [json.dumps(header, ensure_ascii=False, separators=(',', ':'), sort_keys=True),
             'core（以下每行依次为真实uid | speaker_local_id | start_ms—end_ms | 原文text；所有行均属于core，按原文顺序；仅压缩重复字段名，不省略原文）：']
    for s in segments:
        lines.append(f"{s['uid']} | {s['speaker_local_id']} | {s['start_ms']}—{s['end_ms']} | {s['text']}")
    return segments, {'role':'user','content':'\n'.join(lines)}


def event_rows(raw, revision, segments):
    ordinals = {s['uid']:s['ordinal'] for s in segments}
    events = []
    for parent in raw['events']:
        family = [(parent, None), *((child, parent['local_key']) for child in parent['sub_events'])]
        for event, parent_id in family:
            span, = event['ranges']
            if span['source_revision_id'] != revision:
                raise ValueError('event_source_mismatch')
            events.append(dict(id=event['local_key'], parent_id=parent_id, title=event['title'],
                               topics=event.get('topics'), boundary_reason=event['boundary_reason'],
                               start=ordinals[span['start_segment_uid']], end=ordinals[span['end_segment_uid']]))
    Corpus(segments, events, []).validate()
    return events
