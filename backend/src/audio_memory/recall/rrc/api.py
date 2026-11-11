"""Local RRC page API. Single-user RRC with a frozen seed index and updateable transcript revisions."""
import asyncio
import json
import hashlib
import time
import shutil
import numpy as np
from .uploads import Uploads, build
from ..clients import CorporateChatClient
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ConfigDict

from audio_memory.providers.keychain import MacSecurityClient
from ..credentials import RecallCredentials
from ..clients import CHAT_ENDPOINT, CHAT_MODEL, CORPORATE_EMBEDDING_ENDPOINT, CORPORATE_EMBEDDING_MODEL, EmbeddingClient, parse_model_object
from . import engine, index
from .storage import read_json, save_json


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=20000)
    client_message_id: str = Field(min_length=1, max_length=120)


class TranscriptInput(BaseModel):
    name: str = Field(min_length=1,max_length=255)
    recorded_at: datetime
    content: str = Field(min_length=1,max_length=5000000)


def page_router(root: Path, index_dir: Path | None, sources: Path | None, keychain_service: str):
    uploads = None
    def initialize_sync():
        index.configure(index_dir, sources)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return Uploads(root)

    async def initialize():
        nonlocal uploads
        if uploads is None:
            uploads = initialize_sync()

    router = APIRouter(prefix="/api/recall", dependencies=[Depends(initialize)])
    locks = {}
    publication = asyncio.Lock()

    @router.get('/rrc-transcripts')
    async def list_transcripts():
        return uploads.files

    @router.put('/rrc-transcripts/{file_id}')
    async def upload(file_id:UUID, payload:TranscriptInput):
        secret = RecallCredentials(MacSecurityClient(), service=keychain_service).read('corporate').secret
        if not secret:
            raise HTTPException(503,'corporate_model_not_configured')
        async with locks.setdefault(('file',file_id),asyncio.Lock()):
            digest = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
            existing = uploads.files.get(str(file_id))
            if existing and existing.get('digest') == digest:
                return existing
            async with httpx.AsyncClient(trust_env=False,timeout=220) as http:
                token = secret.decode()
                encoder = EmbeddingClient(token,http,provider='corporate_apihub',endpoint=CORPORATE_EMBEDDING_ENDPOINT,revision='rrc-v1',dimension=None,model=CORPORATE_EMBEDDING_MODEL,corporate=True)
                try:
                    data,vectors = await build(payload.content,payload.recorded_at,CorporateChatClient(token,http),encoder)
                except ValueError as exc:
                    raise HTTPException(422,str(exc)) from exc
            revision = str(uuid4())
            folder = root/revision
            folder.mkdir(mode=0o700)
            try:
                save_json(folder/'index.json',data)
                np.save(folder/'vectors.npy',vectors)
                async with publication:
                    previous = uploads.files.get(str(file_id))
                    item = dict(digest=digest,name=payload.name,revision=revision,recorded_at=payload.recorded_at.isoformat())
                    uploads.activate({**uploads.files,str(file_id):item})
            except BaseException:
                shutil.rmtree(folder)
                raise
            if previous:
                shutil.rmtree(root/previous['revision'])
            return item

    @router.delete('/rrc-transcripts/{file_id}')
    async def delete(file_id:UUID):
        async with locks.setdefault(('file',file_id),asyncio.Lock()), publication:
            files = uploads.files.copy()
            previous = files.pop(str(file_id),None)
            if previous:
                uploads.activate(files)
                shutil.rmtree(root/previous['revision'])
            return dict(deleted=bool(previous))


    def load(path):
        if not path.exists():
            raise HTTPException(404)
        return read_json(path)

    def session_path(sid):
        return root / ('session-' + str(sid) + '.json')

    @router.get('/status')
    async def status():
        return dict(configured=True,can_query=bool(index.chunks),ready_retrievable_count=len(index.chunks),
                    ready_event_count=len({(w['sid'],e) for w in index.chunks for e in w['ids']}),
                    strategy='rrc-v1',scope_label='RRC v1 · 已就绪逐字稿索引')

    @router.post('/sessions',status_code=201)
    async def create():
        sid=uuid4()
        save_json(session_path(sid),dict(session_id=str(sid),turns=[]))
        return dict(session_id=sid)

    @router.get('/sessions/{sid}')
    async def session(sid:UUID):
        return load(session_path(sid))

    @router.post('/sessions/{sid}/turns')
    async def submit(sid:UUID, payload:Input):
        if not payload.question.strip():
            raise HTTPException(422)
        async with locks.setdefault(sid,asyncio.Lock()), publication:
            state=load(session_path(sid))
            for turn in state['turns']:
                if turn['message_id']==payload.client_message_id:
                    if turn['question']!=payload.question:raise HTTPException(409)
                    return dict(turn_id=turn['turn_id'],status=turn['status'])
            prev=state['turns'][-1] if state['turns'] else None
            resume=prev and prev['answer']['answer_status']=='needs_clarification'
            now=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            dialogue=list(state['dialogue']) if resume else []
            rounds=list(state['rounds']) if resume else []
            dialogue.append(dict(role='user',text=payload.question))
            tid=str(uuid4());folder=root/tid;folder.mkdir(mode=0o700)
            calls=[];start=time.monotonic()
            secret=RecallCredentials(MacSecurityClient(), service=keychain_service).read('corporate').secret
            if not secret:raise HTTPException(503,'corporate_model_not_configured')
            token=secret.decode()
            async with httpx.AsyncClient(trust_env=False,timeout=220) as http:
                encoder=EmbeddingClient(token,http,provider='corporate_apihub',endpoint=CORPORATE_EMBEDDING_ENDPOINT,revision='rrc-v1',dimension=None,model=CORPORATE_EMBEDDING_MODEL,corporate=True)
                async def model(stage,prompt,data):
                    body=dict(model=CHAT_MODEL,max_tokens=16384,enable_thinking=True,thinking_budget=4096,reasoning_effort='low',parallel_tool_calls=False,response_format={'type':'json_object'},messages=[dict(role='system',content=prompt),dict(role='user',content='输入 JSON：\n'+json.dumps(data,ensure_ascii=False))])
                    res=await http.post(CHAT_ENDPOINT,json=body,headers={'X-CHJ-GWToken':token,'BCS-APIHub-RequestId':str(uuid4())})
                    res.raise_for_status();reply=res.json()
                    calls.append(dict(stage=stage,usage=reply.get('usage')))
                    obj=parse_model_object(reply['choices'][0]['message']['content'])
                    save_json(folder/(stage+'.json'),obj)
                    return obj
                result=await engine.run(dialogue,now,rounds,model,encoder,folder)
            citations=[]
            if result['status']=='awaiting_user':
                text=result['questions'][-1]['question'];status='needs_clarification'
                dialogue.append(dict(role='assistant',text=text))
            else:
                answer=result.get('answer') or {};text=answer.get('answer') or '未找到足够依据'
                pack=read_json(folder/'evidence.json') if (folder/'evidence.json').exists() else {}
                lines={k:v for e in pack.get('evidence',[]) for k,v in e['lines'].items()}
                status='partial' if answer.get('unresolved') else 'complete'
                if not lines:status='insufficient_evidence'
                anchors = {}
                for claim in answer.get('support', []):
                    for ref in claim.get('anchors', []):
                        if not isinstance(ref, str):
                            continue
                        matches = [key for key, line in lines.items() if ref == key or ref == line]
                        if len(matches) == 1:
                            anchors[matches[0]] = None
                for k in anchors:
                    source,position=k.rsplit(':',1);seg=index.sources[source]['segments'][int(position)]
                    citations.append(dict(source_revision_id=source,start_segment_uid=seg['uid'],end_segment_uid=seg['uid'],quote=seg['text'],recorded_date=lines[k][:10]))
            turn=dict(turn_id=tid,question=payload.question,message_id=payload.client_message_id,status='completed',strategy='rrc-v1',answer=dict(text=text,answer_status=status,citations=citations))
            save_json(folder/'turn.json',turn)
            save_json(folder/'trace.json',dict(dialogue=dialogue,rounds=result['rounds'],calls=calls,elapsed=time.monotonic()-start))
            state.update(dialogue=dialogue,rounds=result['rounds']);state['turns'].append(turn)
            save_json(session_path(sid),state)
            return dict(turn_id=tid,status='completed')

    @router.get('/turns/{tid}/citations/{i}/source',response_class=PlainTextResponse)
    async def citation(tid:UUID,i:int):
        refs=load(root/str(tid)/'turn.json')['answer']['citations']
        if not 0<=i<len(refs):raise HTTPException(404)
        ref=refs[i]
        src=index.sources.get(ref['source_revision_id'])
        seg=next((z for z in src['segments'] if z['uid']==ref['start_segment_uid']),None) if src else None
        if not seg:raise HTTPException(404)
        return seg['text']

    @router.get('/turns/{tid}/brief')
    async def brief(tid:UUID):
        trace=load(root/str(tid)/'trace.json')
        return dict(events=[],linked_facts=[],generation_calls=len(trace['calls']),answer_ms=round(1000*trace['elapsed']),brief_model_calls=0,linkage_note='RRC v1 · 逐字稿切片引用见答案。')
    return router
