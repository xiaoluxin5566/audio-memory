"""Snapshot-local facts-first recall. No global DB reads or implicit source access.

The caller supplies a frozen, authorized corpus and an audited model adapter.
This module is used by the evaluation path; the legacy production service remains
unchanged until the new path is evaluated and explicitly selected.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math
import re


def terms(text):
    words = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text.lower())
    return [token for w in words for token in
            ([w] if not re.search(r'[\u4e00-\u9fff]', w) or len(w)<2
             else [w[i:i+2] for i in range(len(w)-1)])]


def rank(items, query, text, limit):
    tokens=[Counter(terms(text(row))) for row in items]
    q=set(terms(query)); n=len(items)
    df=Counter(t for row in tokens for t in row)
    average=sum(map(lambda x:sum(x.values()),tokens))/max(n,1)
    scored=[]
    for item, row in zip(items,tokens):
        length=sum(row.values())
        score=sum(math.log(1+(n-df[t]+0.5)/(df[t]+0.5)) * row[t]*2.2 /
                  (row[t]+1.2*(0.25+0.75*length/max(average,1)))
                  for t in q if row[t])
        if score>0:scored.append((score,item))
    return [item for _,item in sorted(scored,key=lambda pair:-pair[0])[:limit]]


@dataclass
class Corpus:
    segments: list[dict]
    events: list[dict]
    facts: list[dict]
    fact_vectors: dict = field(default_factory=dict)

    def validate(self):
        ordinals={s['ordinal'] for s in self.segments}
        events={e['id']:e for e in self.events}
        if len(events)!=len(self.events) or len({f['id'] for f in self.facts})!=len(self.facts):
            raise ValueError('duplicate_id')
        for event in self.events:
            a,b=event['start'],event['end']
            if a>b or not set(range(a,b+1))<=ordinals:raise ValueError('event_range')
            if event.get('parent_id'):
                parent=events.get(event['parent_id'])
                if not parent or parent.get('parent_id') or not parent['start']<=a<=b<=parent['end']:
                    raise ValueError('event_range')
        for fact in self.facts:
            owner=events.get(fact['owner_id'])
            if not owner or not fact.get('claim_text') or not fact.get('evidence'):raise ValueError('fact_owner')
            for r in fact['evidence']:
                if not owner['start']<=r['start']<=r['end']<=owner['end'] or not set(range(r['start'],r['end']+1))<=ordinals:
                    raise ValueError('fact_range')

    def search_facts(self,query,limit=24,query_vector=None):
        lexical=rank(self.facts,query,lambda f:f['claim_text']+' '+' '.join(f.get('topics',[])),limit*2)
        if query_vector is None:return lexical[:limit]
        if any(len(v)!=len(query_vector) for v in self.fact_vectors.values()):
            raise ValueError('vector_dimension')
        semantic=sorted((f for f in self.facts if f['id'] in self.fact_vectors),
                        key=lambda f:-sum(a*b for a,b in zip(query_vector,self.fact_vectors[f['id']])))[:limit*2]
        scores=Counter()
        for ordered in (lexical,semantic):
            for position,f in enumerate(ordered):scores[f['id']]+=1/(60+position+1)
        by_id={f['id']:f for f in self.facts}
        return [by_id[k] for k,_ in scores.most_common(limit)]

    def select_events(self,query,facts,children,limit=8):
        eligible=[e for e in self.events if bool(e.get('parent_id'))==children]
        by_id={e['id']:e for e in self.events}
        owners={f['owner_id'] for f in facts}
        if not children:
            owners={by_id[x].get('parent_id') or x for x in owners}
        # Search event metadata as well as fact ownership: missing facts must not
        # make an otherwise indexed discussion impossible to locate.
        scored=rank(eligible,query,lambda e:e['title']+' '+' '.join(e.get('topics') or []),limit)
        owned=[e for e in eligible if e['id'] in owners]
        result=[]
        for e in scored+owned:
            if e not in result:result.append(e)
        return result

    def read(self,events):
        seen=set(); result=[]
        for event in events:
            selected=[s for s in self.segments if event['start']<=s['ordinal']<=event['end']]
            fresh=[s for s in selected if s['ordinal'] not in seen]
            seen.update(s['ordinal'] for s in selected)
            result.append(dict(event,segments=fresh,source_ordinals=[s['ordinal'] for s in selected]))
        return result


async def recall(corpus,question,judge,*,query_vector=None):
    corpus.validate()
    facts=corpus.search_facts(question,query_vector=query_vector)
    trace=[]; read=set(); query=question
    last=None
    requested_children=None
    for layer in ('facts','children','parents'):
        if layer=='facts':evidence=facts
        else:
            selected=corpus.select_events(query,facts,children=layer=='children')
            if layer=='children' and requested_children is not None:
                selected=[e for e in corpus.events if e['id'] in requested_children]
            # At final fallback expand to all parents in this explicitly scoped
            # snapshot: absence in top-k candidates alone is not global absence.
            if layer=='parents':selected=[e for e in corpus.events if not e.get('parent_id')]
            evidence=corpus.read(selected)
            read.update(s['ordinal'] for e in evidence for s in e['segments'])
        result=await judge(question,layer,evidence)
        allowed={e['id'] for e in evidence}
        ids=result.get('evidence_ids',[])
        if not set(ids)<=allowed:raise ValueError('unread_evidence')
        if result.get('sufficient') and (not ids or result.get('status')!='complete'):
            raise ValueError('complete_requires_evidence')
        trace.append({'layer':layer,'candidate_ids':[e['id'] for e in evidence],
                      'source_ordinals':sorted({s['ordinal'] for e in evidence for s in e.get('segments',[])}),
                      'decision':result})
        last=result
        if layer=='facts' and 'read_child_ids' in result:
            requested_children=result['read_child_ids']
            children={e['id'] for e in corpus.events if e.get('parent_id')}
            if not isinstance(requested_children,list) or not set(requested_children)<=children:
                raise ValueError('invalid_child_selection')
        if result.get('sufficient') and not (layer=='facts' and result.get('requires_context')):break
        query=question+' '+' '.join(result.get('missing',[]))
    return dict(answer=last['answer'],status=last['status'],layer=layer,
                evidence_ids=last.get('evidence_ids',[]),trace=trace,
                source_ordinals_read=sorted(read))


def scoped_facts(corpus, question, parent_ids, *, query_vector=None, seed_limit=12):
    """Retrieve within activities, then include the seed propositions' qualifications.

    topic_id is extraction-produced and local to an owner, never a benchmark label.
    """
    by_id={e['id']:e for e in corpus.events}
    def parent(f):
        owner=by_id[f['owner_id']]
        return owner.get('parent_id') or owner['id']
    eligible=[f for f in corpus.facts if parent(f) in parent_ids]
    scoped=Corpus(corpus.segments,corpus.events,eligible,
                  {f['id']:corpus.fact_vectors[f['id']] for f in eligible if f['id'] in corpus.fact_vectors})
    seeds=scoped.search_facts(question,limit=seed_limit,query_vector=query_vector)
    keys={(f['owner_id'],f['topic_id']) for f in seeds if f.get('topic_id')}
    ids={f['id'] for f in seeds}
    return seeds+[f for f in eligible if f['id'] not in ids and
                  f.get('topic_id') and (f['owner_id'],f['topic_id']) in keys]


async def recall_scoped(corpus, question, judge, *, plan, query_vector=None):
    """Execute a model-selected activity scope, with summary routing held fixed.

    Planning uses only the question and event metadata. No benchmark categories
    enter this function. Absence is bounded by the recorded searched activities.
    """
    corpus.validate()
    by_id={e['id']:e for e in corpus.events}
    parents=plan.get('parent_ids',[]); children=plan.get('child_ids',[])
    if (not isinstance(parents,list) or not parents or
        any(p not in by_id or by_id[p].get('parent_id') for p in parents) or
        not isinstance(children,list) or
        any(c not in by_id or by_id[c].get('parent_id') not in parents for c in children) or
        plan.get('mode') not in {'fact','summary'}):
        raise ValueError('invalid_scope')
    facts=scoped_facts(corpus,question,parents,query_vector=query_vector)
    trace=[]; read=set()
    for layer in ('facts','children','parents'):
        if layer=='facts':evidence=facts
        else:
            if layer=='children':
                selected=[by_id[c] for c in children]
                if not selected:
                    selected=[e for e in corpus.select_events(question,facts,True)
                              if e['parent_id'] in parents]
            else:selected=[by_id[p] for p in parents]
            if layer=='children' and not selected:
                continue
            evidence=corpus.read(selected)
            read.update(s['ordinal'] for e in evidence for s in e['segments'])
        result=await judge(question,layer,evidence)
        ids=result.get('evidence_ids',[])
        if not isinstance(ids,list) or not set(ids)<={e['id'] for e in evidence}:
            raise ValueError('unread_evidence')
        if result.get('sufficient') and (not ids or result.get('status')!='complete'):
            raise ValueError('complete_requires_evidence')
        trace.append({'layer':layer,'candidate_ids':[e['id'] for e in evidence],
                      'source_ordinals':sorted({s['ordinal'] for e in evidence for s in e.get('segments',[])}),
                      'decision':result})
        if result.get('sufficient') and not (layer=='facts' and plan['mode']=='summary'):break
    return dict(answer=result['answer'],status=result['status'],layer=layer,
                evidence_ids=result.get('evidence_ids',[]),trace=trace,
                source_ordinals_read=sorted(read),plan=plan,search_scope_parent_ids=parents)


def project_spv_fact(raw, fact_id, namespace):
    """Lossless text projection for the existing retrieval interface, not a rewrite."""
    for key in ('subject','property','value','speaker','kind'):
        if not isinstance(raw.get(key),str) or not raw[key].strip():
            raise ValueError('invalid_spv_'+key)
    return dict(raw,id=fact_id,
                claim_text=f"{raw['subject']}—{raw['property']}：{raw['value']}（来源：{raw['speaker']}；性质：{raw['kind']}）",
                topic_id=namespace+'::'+raw['subject']+'::'+raw['property'],
                topics=[raw['subject'],raw['property']])


def validate_aligned_decision(result, evidence, plan):
    """Protocol/source-label checks only; these do not certify semantic accuracy."""
    by_id={e['id']:e for e in evidence}
    ids=result.get('evidence_ids',[])
    if not isinstance(ids,list) or not set(ids)<=set(by_id):raise ValueError('unread_evidence')
    covered=result.get('covered_aspects',[])
    if not isinstance(covered,list) or any(type(i) is not int or not 0<=i<len(plan['aspects']) for i in covered):
        raise ValueError('invalid_aspects')
    basis=result.get('basis',[])
    if not isinstance(basis,list):raise ValueError('invalid_basis')
    cited=set()
    for b in basis:
        refs=b.get('evidence_ids',[])
        if not refs or not set(refs)<=set(by_id):raise ValueError('unread_evidence')
        allowed={'未知'}
        for key in refs:allowed.update(by_id[key].get('source_speakers',[]))
        speakers=b.get('source_speakers',[])
        if not isinstance(speakers,list) or not set(speakers)<=allowed:raise ValueError('unsupported_speaker')
        if not b.get('meaning'):raise ValueError('empty_basis')
        cited.update(refs)
    if not set(ids)<=cited:raise ValueError('answer_without_basis')
    if result.get('sufficient'):
        if not ids or result.get('status')!='complete':raise ValueError('complete_requires_evidence')
        if set(covered)!=set(range(len(plan['aspects']))) or result.get('missing'):
            raise ValueError('incomplete_aspects')
    elif result.get('status')=='complete':raise ValueError('complete_requires_evidence')


async def recall_aligned(corpus, question, judge, *, plan, query_vector=None):
    """Same frozen query plan for both arms; choose child reads after fact retrieval."""
    corpus.validate()
    by_id={e['id']:e for e in corpus.events}
    parents=plan.get('parent_ids',[])
    if (not isinstance(parents,list) or not parents or
        any(p not in by_id or by_id[p].get('parent_id') for p in parents) or
        plan.get('coverage') not in {'focused','discussion'} or
        not isinstance(plan.get('aspects'),list) or not plan['aspects'] or
        any(not isinstance(a,str) or not a.strip() for a in plan['aspects'])):
        raise ValueError('invalid_scope')
    labels={s.get('speaker_local_id') for s in corpus.segments if s.get('speaker_local_id')}
    facts=[]
    for f in scoped_facts(corpus,question,parents,query_vector=query_vector):
        speaker=f.get('speaker','')
        speakers=sorted(label for label in labels if re.search(r'(?<![A-Za-z0-9_])'+re.escape(label)+r'(?![A-Za-z0-9_])',speaker))
        owner=by_id[f['owner_id']]
        facts.append(dict(f,evidence_type='fact',parent_id=owner.get('parent_id') or owner['id'],source_speakers=speakers))
    eligible_children=[e for e in corpus.events if e.get('parent_id') in parents]
    context={'plan':plan,'child_index':[{k:e.get(k) for k in ('id','parent_id','title','topics')} for e in eligible_children]}
    selected_children=[];trace=[];read=set();read_parents=[]
    for layer in ('facts','children','parents'):
        evidence=list(facts)
        if layer!='facts':
            if layer=='children':
                selected=selected_children
                if not selected:continue
            else:
                parent_ids={e['parent_id'] for e in selected_children} or set(parents)
                selected=[by_id[p] for p in parents if p in parent_ids]
                read_parents=[p['id'] for p in selected]
            for e in corpus.read(selected):
                evidence.append(dict(e,evidence_type='source',source_speakers=sorted({s['speaker_local_id'] for s in e['segments'] if s.get('speaker_local_id')})))
                read.update(s['ordinal'] for s in e['segments'])
        result=await judge(question,layer,evidence,context)
        validate_aligned_decision(result,evidence,plan)
        trace.append({'layer':layer,'candidate_ids':[e['id'] for e in evidence],
                      'source_ordinals':sorted({s['ordinal'] for e in evidence for s in e.get('segments',[])}),
                      'decision':result})
        if layer=='facts':
            requested=result.get('read_child_ids',[])
            child_ids={e['id'] for e in eligible_children}
            if not isinstance(requested,list) or not set(requested)<=child_ids:raise ValueError('invalid_child_selection')
            selected_children=[e for e in eligible_children if e['id'] in requested]
            if not selected_children and (not result.get('sufficient') or plan['coverage']=='discussion'):
                selected_children=[e for e in corpus.select_events(question,facts,True) if e.get('parent_id') in parents]
        if result.get('sufficient') and not (layer=='facts' and plan['coverage']=='discussion'):break
    return dict(answer=result['answer'],status=result['status'],layer=layer,evidence_ids=result.get('evidence_ids',[]),
                trace=trace,source_ordinals_read=sorted(read),plan=plan,read_parent_ids=read_parents)
