import re,time
from datetime import datetime,timedelta
from pathlib import Path
from .. import index
from ..engine import calendar,passage,neighbor,build_package
from ..storage import save_json,read_json
from .schema import validate_answer
from .safeguards import guard_plan,normalize_read_result
slot=(Path(__file__).parent/'plan-prompt.txt').read_text()
react=(Path(__file__).parent/'read-answer-prompt.txt').read_text()

async def run(dialogue, as_of, rounds, model, enc, folder, *, reader_prompt=react, v4=False):
    start=time.monotonic();expanded=False;questions=[];plans=[];visible={};aliases={};notes=[]
    def save(k,x):save_json(folder/(k+'.json'),x)
    def end(ans,status):
        return dict(answer=ans,status=status,dialogue=dialogue,questions=questions,rounds=rounds,plans=plans,notes=notes,seconds=time.monotonic()-start)
    async def route():
        p=await model('slots'+str(len(dialogue)),slot,dict(dialogue=dialogue,rounds=rounds,as_of=as_of,calendar=calendar,protocol='附加工程字段 start_date,end_date 为YYYY-MM-DD，afternoon为布尔，on_empty为ask_time或expand；clarification_question填实际要问的中文（含零命中时的问句）。'))
        if isinstance(p.get('content'),dict):p=p['content']
        for key in ('initial_scope','fallback_scope'):
            value=p.get(key)
            if isinstance(value,str):
                dates=re.findall(r'\d{4}-\d{2}-\d{2}',value)
                if not dates:dates=[as_of[:4]+'-'+v for v in re.findall(r'(?<![0-9-])\d{2}-\d{2}(?![0-9-])',value)]
                if len(dates)==2:p[key]=dict(start_date=dates[0],end_date=dates[1])
        if isinstance(p.get('initial_scope'),dict):p={**p['initial_scope'],**p}
        p,audit=guard_plan(p,dialogue,as_of)
        save('plan-guard'+str(len(dialogue)),audit)
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
    if p.get('action')=='clarify' and not (rounds and len(dialogue)>1 and p.get('start_date') and p.get('end_date')):
        return clarify(p)
    async def request():
        return dict(action='retrieve',query=p.get('query') or ' '.join(d['text'] for d in dialogue if d['role']=='user'),clues=p.get('clues',[]))
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
        data=dict(dialogue=dialogue,plan=p,rounds=rounds,passages=text,remaining=5-stage,protocol='每次一个action。finish时返回final_answer，内含answer、support、unresolved；support每项包含claim与anchors（已展示的独立L行号数组）；需要继续时使用before/after和anchor，或search_again和query、clues。report_result若范围内确无对应事件请写result_status=no_match。')
        if v4:
            data['protocol'] += '。finish还须在final_answer中填写has_supported_answer和citation_coverage，按系统提示定义。'
        async def read_checked(name,payload):
            obj=await model(name,reader_prompt,payload)
            obj,_=normalize_read_result(obj)
            ans=obj.get('final_answer')
            if isinstance(ans,dict) and isinstance(ans.get('support'),list):
                for entry in ans['support']:
                    if isinstance(entry,dict) and '.anchors' in entry and 'anchors' not in entry:
                        entry['anchors']=entry.pop('.anchors')
            if obj.get('action')=='finish' or 'final_answer' in obj:
                validate_answer(ans,set(aliases.values()))
                if v4:
                    from ..v4.status import validate
                    validate(ans, retry_missing=not name.endswith('retry'))
            return obj
        try:
            decision=await read_checked('react'+str(stage),data)
        except ValueError as error:
            retry={**data,'format_error':str(error),'protocol':data['protocol']+'。请依据本次展示的原文重新作答。引用只用实际展示的L行号；逐项生成claim与anchors对象。'}
            decision=await read_checked('react'+str(stage)+'retry',retry)
        if decision.get('action')=='finish' or 'final_answer' in decision:
            validate_answer(decision.get('final_answer'),set(aliases.values()))
            decision['events']=[dict(point=v['claim'],lines=v['anchors']) for v in decision['final_answer'].get('support',[])]
        observed.extend(build_package(decision,aliases,visible)['evidence'])
        act=decision.get('action',decision.get('type','finish'))
        if act=='report_result' and decision.get('result_status') in ('no_match','no_hit'):
            rounds[-1]['match_status']='no_match'
            save('rounds',rounds)
            if not expanded and p.get('on_empty')=='expand' and stage<5:
                p=dict(p,start_date=(datetime.fromisoformat(p['start_date'])-timedelta(days=2)).date().isoformat(),end_date=(datetime.fromisoformat(p['end_date'])+timedelta(days=2)).date().isoformat())
                expanded=True;visible={};aliases={};observed=[];a=await request();continue
        if act=='report_result' and decision.get('result_status') in ('no_match','no_hit') and (expanded or p.get('on_empty')=='ask_time') and stage<5:
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
    ans=decision.get('final_answer')
    validate_answer(ans,set(aliases.values()))
    lookup={v:k for k,v in aliases.items()}
    for item in ans['support']:item['anchors']=[lookup[v] for v in item['anchors']]
    save('answer',ans)
    return end(ans,'partial_at_limit' if notes and notes[-1].get('terminal')=='round_limit' else 'answered')
