import json,re,time
from datetime import datetime,timedelta
from pathlib import Path
from . import index
from .storage import read_json,save_json
PROMPTS=Path(__file__).resolve().parent
slot,react,answer=[(PROMPTS/(name+'.txt')).read_text() for name in ('slots','react','answer')]
calendar={'七月底/七月最后/七月快结束':'07-25..07-31','八月初/刚进八月/八月头一个星期':'08-01..08-07','八月快到十号/八月上旬快结束':'08-07..08-10','八月中旬刚开始':'08-11..08-14','八月过半':'08-13..08-17','八月中旬靠后':'08-16..08-20','八月中旬':'08-11..08-20','下午':'12:00..18:00'}


def passage(scoped,aliases):
    blocks=[]
    for i,lines in scoped.items():
        w=index.chunks[i]
        base=datetime.fromisoformat(next(iter(lines.values())).split(' ',1)[0])
        block=[f"片{i} 来源{w['sid']} 事件{','.join(w['ids'])} 起点{base.isoformat()}"]
        for k,v in lines.items():
            stamp,utterance=v.split(' ',1)
            aliases.setdefault(k,f'L{len(aliases)+1}')
            block.append(f"{aliases[k]} +{int((datetime.fromisoformat(stamp)-base).total_seconds())} {utterance}")
        blocks.append('\n'.join(block))
    return '\n\n'.join(blocks)

def neighbor(action, visible, aliases, scoped):
    anchor=str(action.get('anchor',''))
    label=re.search(r'(?<![A-Za-z0-9])L(\d+)',anchor)
    chunk=re.search(r'片\s*(\d+)',anchor)
    key=next((k for k,v in aliases.items() if label and v==label[0]),None)
    matches=[i for i,rows in visible.items() if key in rows and (not chunk or i==int(chunk[1]))]
    if len(matches)!=1:return [],'invalid_anchor'
    base=matches[0];step=1 if action['action']=='after' else -1
    i=base+step
    while i in scoped and index.chunks[i]['sid']==index.chunks[base]['sid'] and set(index.chunks[i]['ids'])&set(index.chunks[base]['ids']):
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

async def run(dialogue, as_of, rounds, model, enc, folder):
    start=time.monotonic();expanded=False;questions=[];plans=[];visible={};aliases={};notes=[]
    def save(k,x):save_json(folder/(k+'.json'),x)
    def end(ans,status):
        return dict(answer=ans,status=status,dialogue=dialogue,questions=questions,rounds=rounds,plans=plans,notes=notes,seconds=time.monotonic()-start)
    async def route():
        p=await model('slots'+str(len(dialogue)),slot,dict(dialogue=dialogue,rounds=rounds,as_of=as_of,calendar=calendar,protocol='附加工程字段 start_date,end_date 为YYYY-MM-DD，afternoon为布尔，on_empty为ask_time或expand；clarification_question填实际要问的中文（含零命中时的问句）。'))
        p=p.get('content',p)
        for key in ('initial_scope','fallback_scope'):
            value=p.get(key)
            if isinstance(value,str):
                dates=re.findall(r'(?:\d{4}-)?\d{2}-\d{2}',value)
                if len(dates)==2:
                    year=as_of[:4]
                    lo,hi=[v if len(v)==10 else year+'-'+v for v in dates]
                    p[key]=dict(start_date=lo,end_date=hi)
        plans.append(p);return p
    def clarify(p):
        question=p.get('clarification_question') or p.get('question') or ''
        if not question and rounds:
            branch=p.get('on_fallback_no_hit') if expanded else p.get('on_no_hit')
            if branch:
                question='你还记得大概是什么时候聊到这件事的吗？'

        if not isinstance(question,str) or not question.strip():question='你想回忆哪件事，大概是什么时候？'
        questions.append(dict(after=len(rounds),question=question))
        return end(None,'awaiting_user')
    p=await route()
    if p.get('action')=='clarify':
        return clarify(p)
    async def request():
        return await model('request'+str(len(rounds)),react,dict(dialogue=dialogue,plan=p,rounds=rounds,protocol='仅返回 {"action":"retrieve","query":"检索词","clues":["可区分的正文短词"]}。'))
    if len(rounds) >= 5:
        return end(None,'round_limit_without_evidence')
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
        ids = index.scoped_ids(prevdate,p['end_date'])
        for i in ids:
            rows=index.entries(i,p)
            if rows:scoped[i]=rows
        selected=[]; audit=[]
        if 'action' not in a and 'type' in a:a={**a,'action':a['type']}
        if (folder/f'selection{stage}.json').exists():
            selected=read_json(folder/f'selection{stage}.json')
        elif a.get('action') in ('before','after'):
            selected,reason=neighbor(a,visible,aliases,scoped)
            notes.append(dict(round=stage,neighbor=reason))
        elif scoped:
            query=a.get('query') or dialogue[0]['text']
            v=index.np.array((await enc.embed([query],'event_query')).indexed_vectors[0]);v/=index.np.linalg.norm(v)
            scores=index.matrix@v;best={}
            for k,(i,_) in enumerate(index.blocks):best[i]=max(best.get(i,-1),float(scores[k]))
            ids=list(scoped);allowed=set(a.get('event_ids') or [])
            if allowed:ids=[i for i in ids if allowed.intersection(index.chunks[i]['ids'])]
            texts=['\n'.join(v.split(' ',2)[-1] for v in scoped[i].values()) for i in ids]
            lex=[ids[k] for k in index.BM25(texts).top(query)] if ids else []
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
            return clarify(p)
        visible.update({i:scoped[i] for i in selected});text=passage(visible,aliases)
        data=dict(dialogue=dialogue,plan=p,rounds=rounds,passages=text,remaining=5-stage,protocol='每次一个action。finish采用JSON结构 {"action":"finish","events":[{"event_id":"已有事件ID","time":"原文时间","point":"有据内容","lines":["L1"]}],"gaps":[]}。lines只填实际已读行的L编号；需要继续时使用before/after和anchor，或search_again和query、clues。每次阅读必须另填target_event_status，取hit、no_match或uncertain。按目标事件的识别线索判断，已定位事件但缺所问事实仍为hit。候选原文均与目标事件无关时直接报告no_match；有具体待核实线索才继续取证。不要向用户提问。')
        decision=await model('react'+str(stage),react,data)
        observed.extend(build_package(decision,aliases,visible)['evidence'])
        act=decision.get('action',decision.get('type','finish'))
        match = decision.get('target_event_status') or decision.get('result_status')
        rounds[-1]['match_status'] = match
        save('rounds',rounds)
        if match in ('no_match','no_hit'):
            if stage >= 5:
                return end(None,'round_limit_without_evidence')
            if not expanded and p['on_empty']=='expand':
                p=dict(p,start_date=(datetime.fromisoformat(p['start_date'])-timedelta(days=2)).date().isoformat(),end_date=(datetime.fromisoformat(p['end_date'])+timedelta(days=2)).date().isoformat())
                expanded=True;visible={};aliases={};observed=[];a=await request();continue
            if expanded or p['on_empty']=='ask_time':
                return clarify(p)
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
    answer_plan = {k:p[k] for k in ('original_question','goal','required_points','answer_mode') if k in p}
    ans=await model('answer',answer,dict(dialogue=dialogue,plan=answer_plan,package=pack))
    return end(ans,'partial_at_limit' if notes and notes[-1].get('terminal')=='round_limit' else 'answered')
