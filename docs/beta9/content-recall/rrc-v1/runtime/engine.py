import json,re,sqlite3,time
from datetime import datetime,timedelta
from pathlib import Path
import index
from storage import read_json,save_json
PROMPTS=Path(__file__).resolve().parent.parent/'aon-rag-clarification-repair'
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

async def run(card,http,enc,OUT):
    n=card['sheet_row'];folder=OUT/f'r{n}';folder.mkdir(exist_ok=True)
    if (folder/'result.json').exists():return
    start=time.monotonic();dialogue=[dict(role='user',text=card['question'])];rounds=[];questions=[];plans=[];visible={};aliases={};notes=[]
    def save(k,x):save_json(folder/(k+'.json'),x)
    def end(ans,status):
        save('result',dict(answer=ans,status=status,dialogue=dialogue,questions=questions,rounds=rounds,plans=plans,notes=notes,seconds=time.monotonic()-start))
    async def model(stage,prompt,data):
        if (folder/(stage+'.json')).exists() and (folder/(stage+'-input.json')).exists() and read_json(folder/(stage+'-input.json'))==json.loads(json.dumps(data)):
            return read_json(folder/(stage+'.json'))
        save(stage+'-input',data)
        obj=http.response(stage,prompt,data)
        save(stage,obj)
        return obj
    async def route():
        p=await model('slots'+str(len(dialogue)),slot,dict(dialogue=dialogue,rounds=rounds,as_of=card['as_of'],calendar=calendar,protocol='附加工程字段 start_date,end_date 为YYYY-MM-DD，afternoon为布尔，on_empty为ask_time或expand；clarification_question填实际要问的中文（含零命中时的问句）。'))
        p=p.get('content',p)
        for key in ('initial_scope','fallback_scope'):
            value=p.get(key)
            if isinstance(value,str):
                dates=re.findall(r'(?:\d{4}-)?\d{2}-\d{2}',value)
                if len(dates)==2:
                    year=card['as_of'][:4]
                    lo,hi=[v if len(v)==10 else year+'-'+v for v in dates]
                    p[key]=dict(start_date=lo,end_date=hi)
        plans.append(p);return p
    def clarify(p):
        question=p.get('clarification_question') or p.get('question') or ''
        if not question and rounds:
            branch=p.get('on_fallback_no_hit') if expanded else p.get('on_no_hit')
            if branch:
                question='你还记得大概是什么时候聊到这件事的吗？'

        if not isinstance(question,str):question=str(question)
        questions.append(dict(after=len(rounds),question=question))
        if not question or not card['followup'] or len(dialogue)>1:return False
        # A frozen user response may add context beyond the requested slot.
        # Require an actual information request, then preserve its complete frozen text.
        if not re.search(r'哪|什么|何时|几号|多久|补充|主题|大概时间',question):return False
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
        with sqlite3.connect(f'file:{index.INDEX}/meta.sqlite?mode=ro',uri=True) as db:
            ids=[i for i, in db.execute('select id from chunks where date between ? and ? order by id',(prevdate,p['end_date']))]
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
            query=a.get('query') or card['question']
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
