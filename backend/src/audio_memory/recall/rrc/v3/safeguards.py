"""Enforce existing scope rules and normalize syntax, never invent answers."""
import copy,re
from datetime import datetime,timedelta

def guard_plan(plan,dialogue,as_of):
 p=copy.deepcopy(plan);audit=[];users=[d['text'] for d in dialogue if d['role']=='user'];slots=p.setdefault('slots',{})
 quote=p.get('time_quote') or ''
 if quote and not any(quote in text for text in users):raise ValueError('time_quote_not_in_user_dialogue')
 if quote and (quote.strip() in {'当时','那时','那次','那天','那时候','那个时候','当初','之前','此前','那会儿','过去','以前'} or not re.search(r'[0-9年月日号周天]|最近|昨天|前天|今天|上旬|中旬|下旬|月初|月中|月底|月末|星期|周末|礼拜',quote)):
  audit.append('discard_unanchored_time_reference');quote=''
 p['time_quote']=quote
 slots['time']=quote
 event=bool(slots.get('event'));person=bool(slots.get('person'));place=bool(slots.get('place'))
 if not quote:
  if event or (person and place):
   date=datetime.fromisoformat(as_of).date();expected=((date-timedelta(days=2)).isoformat(),date.isoformat())
   if (p.get('action'),p.get('start_date'),p.get('end_date'),p.get('on_empty'))!=('retrieve',*expected,'ask_time'):audit.append('restore_recent3_then_ask_time')
   p.update(action='retrieve',start_date=expected[0],end_date=expected[1],on_empty='ask_time',clarification_question='你还记得大概是什么时候聊到这件事的吗？')
  else:
   p.update(action='clarify',clarification_question='你想回忆哪件事，大概是什么时候讨论的？')
 elif not event:
  if person!=place:
   p.update(action='clarify',clarification_question='你想回忆的是哪件事或什么主题？')
  elif p.get('action')=='retrieve':p.update(answer_mode='recap',on_empty='ask_time')
 elif p.get('action')=='retrieve':p['on_empty']='expand'
 afternoon_quote=p.get('afternoon_quote') or ''
 backed=bool(afternoon_quote and any(afternoon_quote in text for text in users) and any(w in afternoon_quote for w in ('下午','午后')))
 if p.get('afternoon') and not backed:
  audit.append('remove_unbacked_afternoon');p['query']=str(p.get('query') or '').replace('下午','').replace('午后','').strip()
 p['afternoon']=bool(p.get('afternoon') and backed)
 if p.get('action')=='retrieve':
  start=datetime.fromisoformat(p['start_date']).date();end=datetime.fromisoformat(p['end_date']).date()
  if end<start:raise ValueError('reversed_scope')
  if not p.get('query'):p['query']=' '.join([str(slots.get('event') or ''),str(p.get('goal') or '')]).strip() or ' '.join(users)
 return p,audit

def normalize_read_result(raw):
 obj=copy.deepcopy(raw);audit=[]
 # Exactly the same answer payload may be moved to its canonical envelope.
 if 'action' not in obj and isinstance(obj.get('answer'),str) and 'support' in obj and 'unresolved' in obj:
  return dict(action='finish',final_answer=obj),['wrap_flat_answer']
 final=obj.get('final_answer')
 if isinstance(final,dict):
  for field in ('answer','support','unresolved'):
   if field in obj:
    if field in final and obj[field]!=final[field]:raise ValueError('conflicting_answer_fields')
    if field not in final:final[field]=obj[field];audit.append('move_'+field+'_inside')
    obj.pop(field)
 return obj,audit
