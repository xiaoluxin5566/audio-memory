"""Frozen Sheet2 batch, isolated from previous experiments."""
import asyncio
import gzip
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime,timedelta
from pathlib import Path

import argparse
import fcntl

HERE=Path(__file__).resolve().parent
OUT=HERE
PILOT=HERE.parent/'aon-four-slot-two'
sys.path.insert(0,str(PILOT))
import importlib.util
spec=importlib.util.spec_from_file_location('frozen_pilot',PILOT/'run.py')
pilot=importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)
c=pilot.c
c.R=OUT
slot=(HERE/'slots.txt').read_text()
react=(HERE/'react.txt').read_text()
answer=(HERE/'answer.txt').read_text()
calendar={'七月底/七月最后/七月快结束':'07-25..07-31','八月初/刚进八月/八月头一个星期':'08-01..08-07','八月快到十号/八月上旬快结束':'08-07..08-10','八月中旬刚开始':'08-11..08-14','八月过半':'08-13..08-17','八月中旬靠后':'08-16..08-20','八月中旬':'08-11..08-20','下午':'12:00..18:00'}


def passage(scoped,aliases):
    blocks=[]
    for i,lines in scoped.items():
        w=pilot.chunks[i]
        base=datetime.fromisoformat(next(iter(lines.values())).split(' ',1)[0])
        block=[f"片{i} 来源{w['sid']} 事件{','.join(w['ids'])} 起点{base.isoformat()}"]
        for k,v in lines.items():
            stamp,utterance=v.split(' ',1)
            aliases.setdefault(k,f'L{len(aliases)+1}')
            block.append(f"{aliases[k]} +{int((datetime.fromisoformat(stamp)-base).total_seconds())} {utterance}")
        blocks.append('\n'.join(block))
    return '\n\n'.join(blocks)

def compatible(question,required):
    asks_time=re.search(r'时间|什么时候|哪天|哪几天|几号|时段|日期|何时|哪一天|哪段|哪一周',question)
    event_question=re.sub(r'什么时间|什么时候|哪个时间段|哪个时段|哪次时间','',question)
    asks_event=re.search(r'具体指|指的是什么|买(?:了|的)?什么|聊(?:的|了)?什么|练什么|(?:哪(?:一)?(?:个|件|种|项|门|次|场)|什么|哪个具体)(?:[^，。？?]{0,6})(?:对象|事件|活动|事|东西|项目|课程|内容|练习|话题|主题|购物|买|交流|讨论)|(?:事件|话题|主题|课程|内容|练习)(?:是|指)?什么',event_question)
    asked={name for name,hit in [('time',asks_time),('event',asks_event)] if hit}
    return bool(required) and set(required)<=asked

def neighbor(action, visible, aliases, scoped):
    anchor=str(action.get('anchor',''))
    label=re.search(r'(?<![A-Za-z0-9])L(\d+)',anchor)
    chunk=re.search(r'片\s*(\d+)',anchor)
    key=next((k for k,v in aliases.items() if label and v==label[0]),None)
    matches=[i for i,rows in visible.items() if key in rows and (not chunk or i==int(chunk[1]))]
    if len(matches)!=1:return [],'invalid_anchor'
    base=matches[0];step=1 if action['action']=='after' else -1
    i=base+step
    while i in scoped and pilot.chunks[i]['sid']==pilot.chunks[base]['sid'] and set(pilot.chunks[i]['ids'])&set(pilot.chunks[base]['ids']):
        if i not in visible:return [i],'neighbor'
        i+=step
    return [],'no_eligible_neighbor'

def build_package(decision,aliases,visible):
    evidence=[];lookup={v:k for k,v in aliases.items()};lines={k:v for rows in visible.values() for k,v in rows.items()}
    for event in decision.get('events',decision.get('evidence',[])):
        if not isinstance(event,dict):continue
        for item in event.get('points',[event]):
            if not isinstance(item,dict):continue
            refs=item.get('lines',[]);refs=[refs] if isinstance(refs,str) else refs
            keys=[]
            for ref in refs:
                for l,r in re.findall(r'L(\d+)(?:\s*[-–~至]\s*L?(\d+))?',str(ref)):
                    keys.extend(lookup[f'L{k}'] for k in range(int(l),min(int(r or l),len(lookup))+1) if f'L{k}' in lookup)
            if keys:evidence.append(dict(event_id=event.get('event_id'),time=event.get('time'),point=item.get('point',''),lines={k:lines[k] for k in keys}))
    pack=dict(evidence=evidence,status=decision.get('evidence_status',decision.get('status')),gaps=decision.get('gaps',[]))
    return pack

async def run(card,http,enc,token):
    n=card['sheet_row'];folder=OUT/f'r{n}';folder.mkdir(exist_ok=True)
    if (folder/'result.json').exists():return
    start=time.monotonic();dialogue=[dict(role='user',text=card['question'])];rounds=[];questions=[];plans=[];visible={};aliases={};notes=[]
    def save(k,x):c.save(folder/(k+'.json'),x)
    def end(ans,status):
        save('result',dict(answer=ans,status=status,dialogue=dialogue,questions=questions,rounds=rounds,plans=plans,notes=notes,seconds=time.monotonic()-start))
    async def model(stage,prompt,data):
        if (folder/(stage+'.json')).exists() and (folder/(stage+'-input.json')).exists() and c.read(folder/(stage+'-input.json'))==json.loads(json.dumps(data)):
            return c.read(folder/(stage+'.json'))
        c.scope.set(f'r{n}/{stage}')
        body={**pilot.CHAT_OPTIONS,'max_tokens':16384,'messages':[dict(role='system',content=prompt),dict(role='user',content='输入 JSON：\n'+json.dumps(data,ensure_ascii=False))]}
        (folder/(stage+'.json')).unlink(missing_ok=True)
        save(stage+'-input',data)
        res=await http.post(c.CHAT_ENDPOINT,json=body,headers={'X-CHJ-GWToken':token,'BCS-APIHub-RequestId':str(c.uuid.uuid4())},timeout=220)
        res.raise_for_status();obj=pilot.parse_model_object(res.json()['choices'][0]['message']['content']);save(stage,obj);return obj
    async def route():
        p=await model('slots'+str(len(dialogue)),slot,dict(dialogue=dialogue,rounds=rounds,as_of=card['as_of'],calendar=calendar,protocol='附加工程字段 start_date,end_date 为YYYY-MM-DD，afternoon为布尔，on_empty为ask_time或expand；clarification_question填实际要问的中文（含零命中时的问句）。'))
        plans.append(p);return p
    def clarify(p):
        question=p.get('clarification_question') or p.get('question') or ''
        if not isinstance(question,str):question=str(question)
        questions.append(dict(after=len(rounds),question=question))
        if not question or not card['followup'] or len(dialogue)>1:return False
        if not compatible(question,card['required_slots']):return False
        dialogue.extend([dict(role='assistant',text=question),dict(role='user',text=card['followup'])]);return True
    p=await route()
    while p.get('action')=='clarify':
        if not clarify(p):return end(None,'awaiting_user')
        p=await route()
    async def request():
        return await model('request'+str(len(rounds)),react,dict(dialogue=dialogue,plan=p,rounds=rounds,protocol='仅返回 {"action":"retrieve","query":"检索词","clues":["可区分的正文短词"]}。'))
    a=await request();expanded=False;decision={};observed=[]
    while len(rounds)<5:
        stage=len(rounds)+1
        inner=p.get('initial_scope')
        if isinstance(inner,list) and len(inner)>=2:
            inner=dict(start_date=inner[0],end_date=inner[1],afternoon=inner[2] if len(inner)>2 else False)
        if isinstance(inner,dict):
            p={**inner,**p}
        p.setdefault('afternoon',False)
        if not p.get('on_empty'):
            p['on_empty']='expand' if p.get('fallback_scope') else 'ask_time'
        scoped={}
        prevdate=(datetime.fromisoformat(p['start_date'])-timedelta(days=1)).date().isoformat()
        with sqlite3.connect(f'file:{pilot.INDEX}/meta.sqlite?mode=ro',uri=True) as db:
            ids=[i for i, in db.execute('select id from chunks where date between ? and ? order by id',(prevdate,p['end_date']))]
        for i in ids:
            rows=pilot.entries(i,p)
            if rows:scoped[i]=rows
        selected=[]; audit=[]
        if 'action' not in a and 'type' in a:a={**a,'action':a['type']}
        if (folder/f'selection{stage}.json').exists():
            selected=c.read(folder/f'selection{stage}.json')
        elif a.get('action') in ('before','after'):
            selected,reason=neighbor(a,visible,aliases,scoped)
            notes.append(dict(round=stage,neighbor=reason))
        elif scoped:
            query=a.get('query') or card['question']
            c.scope.set(f'r{n}/embedding{stage}');v=pilot.np.array((await enc.embed([query],'event_query')).indexed_vectors[0]);v/=pilot.np.linalg.norm(v)
            scores=pilot.matrix@v;best={}
            for k,(i,_) in enumerate(pilot.blocks):best[i]=max(best.get(i,-1),float(scores[k]))
            ids=list(scoped);allowed=set(a.get('event_ids') or [])
            if allowed:ids=[i for i in ids if allowed.intersection(pilot.chunks[i]['ids'])]
            texts=['\n'.join(v.split(' ',2)[-1] for v in scoped[i].values()) for i in ids]
            lex=[ids[k] for k in pilot.BM25(texts).top(query)] if ids else []
            sem=sorted(ids,key=lambda i:(-best[i],i))[:24];rrf={}
            for lane in (lex,sem):
                for rank,i in enumerate(lane,1):rrf[i]=rrf.get(i,0)+1/(60+rank)
            ranked=sorted(rrf,key=lambda i:(-rrf[i],i))
            selected=ranked[:10]
            audit=[dict(chunk=i,cosine=best[i],rrf=rrf[i],lexical_rank=lex.index(i)+1 if i in lex else None,vector_rank=sem.index(i)+1 if i in sem else None,selected=i in selected) for i in ranked]
        if not (folder/f'selection{stage}.json').exists():save(f'filter{stage}',audit);save(f'selection{stage}',selected)
        rounds.append(dict(round=stage,scope=[p['start_date'],p['end_date'],p.get('afternoon')],action=a,coverage="recorded" if scoped else "no_recording",eligible=len(scoped),chunks=selected))
        save('rounds',rounds)
        if not visible and not selected and not expanded and p.get('on_empty')=='expand' and stage<5:
            p=dict(p,start_date=(datetime.fromisoformat(p['start_date'])-timedelta(days=2)).date().isoformat(),end_date=(datetime.fromisoformat(p['end_date'])+timedelta(days=2)).date().isoformat());expanded=True;continue
        if not visible and not selected and (expanded or p.get('on_empty')=='ask_time'):
            if not clarify(p):return end(None,'awaiting_user')
            p=await route();expanded=False;a=await request();continue
        visible.update({i:scoped[i] for i in selected});text=passage(visible,aliases)
        data=dict(dialogue=dialogue,plan=p,rounds=rounds,passages=text,remaining=5-stage,protocol='每次一个action。finish时events为数组，每项point和lines（短锚点数组）；需要继续时使用before/after和anchor，或search_again和query、clues。report_result若范围内确无对应事件请写result_status=no_match。')
        decision=await model('react'+str(stage),react,data)
        observed.extend(build_package(decision,aliases,visible)['evidence'])
        act=decision.get('action',decision.get('type','finish'))
        if act=='report_result' and decision.get('result_status') in ('no_match','no_hit'):
            rounds[-1]['match_status']='no_match'
            save('rounds',rounds)
            if not expanded and p.get('on_empty')=='expand' and stage<5:
                p=dict(p,start_date=(datetime.fromisoformat(p['start_date'])-timedelta(days=2)).date().isoformat(),end_date=(datetime.fromisoformat(p['end_date'])+timedelta(days=2)).date().isoformat())
                expanded=True;visible={};aliases={};observed=[];a=await request();continue
        if act=='report_result' and decision.get('result_status') in ('no_match','no_hit') and (expanded or p.get('on_empty')=='ask_time') and stage<5:
            if not clarify(p):return end(None,'awaiting_user')
            p=await route();a=await request();visible={};aliases={};observed=[];continue
        if act in ('before','after','search_again','retrieve') and stage==5:
            notes.append(dict(round=stage,terminal='round_limit',gap=decision.get('gap')))
            if not observed:
                save('last-read',data)
                return end(None,'round_limit_without_evidence')
        if act not in ('before','after','search_again','retrieve') or stage==5:break
        a={**decision,'action':act}
    pack=build_package(decision,aliases,visible)
    if notes and notes[-1].get('terminal')=='round_limit':
        pack=dict(evidence=observed,status='partial',gaps=[decision.get('gap') or 'round_limit'])
    save('evidence',pack)
    ans=await model('answer',answer,dict(dialogue=dialogue,plan=p,package=pack))
    end(ans,'partial_at_limit' if notes and notes[-1].get('terminal')=='round_limit' else 'answered')

def batch_identity():
    for name,expected in c.read(HERE/'sheet-freeze.json')['sha256'].items():
        if hashlib.sha256((HERE/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('frozen sheet changed: '+name)
    files=c.read(HERE/'manifest.json')['sha256']
    for name,expected in files.items():
        if hashlib.sha256((HERE.parents[1]/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('frozen file changed: '+name)
    hashes={name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in ('manifest.json','cards.json','batch.py')}
    hashes['model']=pilot.CHAT_OPTIONS
    for sid in pilot.sources:
        hashes[sid]=hashlib.sha256((pilot.BASE/'sources'/sid/'source.json').read_bytes()).hexdigest()
    return hashes

class ReceiptHTTP(c.GuardHTTP):
    """Recover completed chat receipts and checkpoint successful vector responses per request."""
    async def send(self,request,**kwargs):
        body=json.loads(request.content)
        directory=OUT/'receipts'/c.scope.get()
        for previous in directory.glob('*.request.json.gz'):
            reply=previous.with_name(previous.name.replace('.request.', '.response.'))
            if reply.exists() and json.loads(gzip.decompress(previous.read_bytes()))==body:
                data=json.loads(gzip.decompress(reply.read_bytes()))
                expected=('deepseek-v4-1-flash-260910' if str(request.url)==c.CHAT_ENDPOINT else 'Qwen3-Embedding-8B')
                if data.get('model')==expected and data.get('usage',{}).get('total_tokens'):
                    return c.httpx.Response(200,json=data,request=request)
        res=await super().send(request,**kwargs)
        if res.is_success and str(request.url)!=c.CHAT_ENDPOINT:
            for previous in directory.glob('*.request.json.gz'):
                if json.loads(gzip.decompress(previous.read_bytes()))==body:
                    reply=previous.with_name(previous.name.replace('.request.', '.response.'))
                    temp=reply.with_suffix('.partial')
                    c.storeblob(temp,res.content);temp.replace(reply)
                    break
        return res

async def batch(args):
    global OUT
    identity=batch_identity()
    cards=c.read(HERE/'cards.json')['cards']
    if args.rows:
        chosen=set(args.rows)
        cards=[card for card in cards if card['sheet_row'] in chosen]
        if len(cards)!=len(chosen):raise ValueError('unknown sheet row')
    if not args.execute:
        print('offline preflight:',len(cards),'cards');return
    if not args.budget or args.budget<1:raise ValueError('supply approved token budget')
    OUT=HERE/'runs'/hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    OUT.mkdir(parents=True,exist_ok=True);c.R=OUT
    with (OUT/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        c.save(OUT/'identity.json',identity)
        c.save(OUT/'authorization.json',dict(shared_token_limit=args.budget))
        async with ReceiptHTTP(trust_env=False,follow_redirects=False) as http:
            chat,enc=c.clients(http)
            for card in cards:
                folder=OUT/f"r{card['sheet_row']}"
                try:
                    await run(card,http,enc,chat.token)
                except Exception as err:
                    folder.mkdir(exist_ok=True)
                    c.save(folder/'failure.json',dict(error=type(err).__name__,detail=str(err)[:500]))
                    if 'budget' in str(err) or (OUT/'STOP').exists():break
                else:
                    (folder/'failure.json').unlink(missing_ok=True)
        summary={}
        for card in cards:
            folder=OUT/f"r{card['sheet_row']}"
            summary[card['sheet_row']]=c.read(folder/'result.json')['status'] if (folder/'result.json').exists() else 'failed' if (folder/'failure.json').exists() else 'pending'
        c.save(OUT/'status.json',summary)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--budget',type=int)
    parser.add_argument('--rows',nargs='+',type=int)
    asyncio.run(batch(parser.parse_args()))
