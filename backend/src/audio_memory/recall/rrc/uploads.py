"""Build an uploaded revision before publishing its manifest to the local RRC page."""
from datetime import datetime
from itertools import groupby
from pathlib import Path
import re
from uuid import uuid4

import numpy as np
from ..aon_baseline_input import segmentation_input, event_rows
from ..clients import CorporateChatClient, parse_model_object
from ..transcript_parser import TranscriptParser
from . import index
from .storage import read_json, save_json

HERE = Path(__file__).parent


async def build(content, recorded_at, chat: CorporateChatClient, encoder):
    transcript = TranscriptParser().parse(content, 'markdown')
    chunks, blocks, sources = [], [], {}
    for section in transcript.sources:
        if not section.segments:
            continue
        sid = str(uuid4())
        # A supplied recording start applies to a single recording. Daily exports
        # carry their individual recording clocks in the section titles.
        stamp = recorded_at
        clock = re.search(r'日\s+(\d\d)-(\d\d)', section.title)
        if clock:
            stamp = stamp.replace(hour=int(clock[1]), minute=int(clock[2]), second=0)
        elif len(transcript.sources) > 1:
            raise ValueError('多录音逐字稿缺少分段录制时间')
        if section.declared_date:
            stamp = datetime.combine(datetime.fromisoformat(section.declared_date).date(), stamp.time())
        segments, user = segmentation_input(dict(source_revision_id=sid, recorded_date=stamp.date().isoformat(),
            recorded_start_at=stamp.isoformat(), segments=[s.model_dump() for s in section.segments]))
        schema = read_json(HERE/'segmentation-schema.json')
        event = schema['properties']['events']['items']['properties']
        for props in (event, event['sub_events']['items']['properties'], schema['properties']['fragments']['items']['properties']):
            span = props['ranges']['items']['properties']
            span['source_revision_id']['const'] = sid
            for key in ('start_segment_uid','end_segment_uid'):
                span[key]['pattern'] = '^' + re.escape(segments[0]['uid'].rsplit(':',1)[0]) + ':[0-9]+$'
        result = await chat.complete([dict(role='system',content=(HERE/'segmentation.txt').read_text()),user],
            tools=[dict(type='function',function=dict(name='submit_structured_result',parameters=schema))],purpose='segmentation')
        events = event_rows(parse_model_object(result.text), sid, segments)
        sources[sid] = dict(name=section.title, recorded_start_at=stamp.isoformat(), segments=segments)
        def owners(i):
            covered = [e for e in events if e['start'] <= i <= e['end']]
            return [e['id'] for e in covered if e['parent_id']] or [e['id'] for e in covered]
        for ids, group in groupby(range(len(segments)), owners):
            positions = list(group)
            for offset in range(0,len(positions),64):
                part = positions[offset:offset+64]
                cid = len(chunks)
                chunks.append(dict(sid=sid,date=stamp.date().isoformat(),ids=ids,start=part[0],end=part[-1]))
                buf = ''
                for i in part:
                    line = segments[i]['speaker_local_id']+' '+segments[i]['text']
                    if buf and len((buf+'\n'+line).encode()) > 8000:
                        blocks.append([cid,buf])
                        buf = ''
                    while len(line.encode()) > 8000:
                        blocks.append([cid,line[:1900]])
                        line = line[1900:]
                    buf = (buf+'\n'+line).strip() if buf else line
                if buf:
                    blocks.append([cid,buf])
    if not blocks:
        raise ValueError('逐字稿没有可检索的原文')
    vectors = []
    for i in range(0,len(blocks),16):
        vectors.extend((await encoder.embed([text for _,text in blocks[i:i+16]],'document')).indexed_vectors)
    matrix = np.asarray(vectors,dtype=index.matrix.dtype)
    norms = np.linalg.norm(matrix,axis=1,keepdims=True)
    if matrix.shape != (len(blocks),index.matrix.shape[1]) or not np.isfinite(norms).all() or (norms==0).any():
        raise ValueError('无效的逐字稿向量')
    return dict(chunks=chunks, blocks=blocks, sources=sources), matrix/norms


class Uploads:
    def __init__(self, root):
        self.root = root
        self.base = index.chunks,index.blocks,index.sources,index.matrix
        self.path = root/'uploads.json'
        self.files = read_json(self.path) if self.path.exists() else {}
        self.activate(self.files, persist=False)

    def activate(self, files, persist=True):
        chunks,blocks,sources,matrix = self.base
        chunks,blocks,sources = chunks.copy(),blocks.copy(),sources.copy()
        matrices = [matrix]
        for item in files.values():
            folder = self.root/item['revision']
            data = read_json(folder/'index.json')
            blocks.extend([i+len(chunks),text] for i,text in data['blocks'])
            chunks.extend(data['chunks'])
            sources.update(data['sources'])
            matrices.append(np.load(folder/'vectors.npy',allow_pickle=False))
        vectors = np.vstack(matrices) if files else matrix
        if persist:
            save_json(self.path,files)
        index.chunks,index.blocks,index.sources,index.matrix = chunks,blocks,sources,vectors
        self.files = files
