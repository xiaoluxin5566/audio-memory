import math,collections,re
from datetime import datetime,timedelta
import numpy as np
from storage import read_json

def configure(index_path,source_path):
    global INDEX,chunks,blocks,matrix,sources
    INDEX=index_path
    chunks=read_json(INDEX/'chunks.json')
    blocks=read_json(INDEX/'blocks.json')
    matrix=np.vstack([np.load(INDEX/f'vectors-{i}.npy',allow_pickle=False) for i in range(0,len(blocks),16)])
    matrix/=np.linalg.norm(matrix,axis=1,keepdims=True)
    sources={x['id']:read_json(source_path/'sources'/x['id']/'source.json') for x in read_json(source_path/'sources.json')}

def terms(text):
    words = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text.lower())
    return [token for w in words for token in
            ([w] if not re.search(r'[\u4e00-\u9fff]', w) or len(w)<2
             else [w[i:i+2] for i in range(len(w)-1)])]

class BM25:
 def __init__(self,texts):
  self.post=collections.defaultdict(list);self.n=len(texts)
  lengths=[]
  for i,t in enumerate(texts):
   bag=collections.Counter(terms(t));lengths.append(sum(bag.values()))
   for word,count in bag.items():self.post[word].append((i,count))
  avg=sum(lengths)/len(lengths);self.scale=[1.2*(0.25+0.75*n/avg) for n in lengths]
 def top(self,query,limit=24):
  scores=collections.defaultdict(float)
  for term in set(terms(query)):
   hits=self.post.get(term,());df=len(hits);idf=math.log(1+(self.n-df+.5)/(df+.5))
   for i,n in hits:scores[i]+=idf*n*2.2/(n+self.scale[i])
  return sorted(scores,key=lambda i:(-scores[i],i))[:limit]

def clock_of(w,z):
    h,m=map(int,re.search(r'日\s+(\d\d)-(\d\d)',sources[w['sid']]['name']).groups())
    return datetime.fromisoformat(w['date'])+timedelta(hours=h,minutes=m,milliseconds=z['start_ms'])

def entries(i,plan):
    w=chunks[i]; out={}
    for n in range(w['start'],w['end']+1):
        z=sources[w['sid']]['segments'][n]; t=clock_of(w,z)
        if not plan['start_date']<=t.date().isoformat()<=plan['end_date']:continue
        if plan['afternoon'] and not 12<=t.hour<18:continue
        out[f"{w['sid']}:{n}"]=f"{t.isoformat(timespec='seconds')} {z['speaker_local_id']} {z['text']}"
    return out
