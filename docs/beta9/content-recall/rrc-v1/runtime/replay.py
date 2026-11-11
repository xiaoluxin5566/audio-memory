"""Standalone offline replay of the RRC query loop."""
import argparse
import asyncio
import hashlib
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import engine
import index
from storage import read_json, save_json

ROOT = Path(__file__).resolve().parent.parent

def verify_prompts():
    expected = read_json(ROOT/'release.json')['test_snapshot']['sha256']
    for name in ('slots', 'react', 'answer'):
        key = f'aon-rag-clarification-repair/{name}.txt'
        digest = hashlib.sha256((ROOT/key).read_bytes()).hexdigest()
        if digest != expected[key]:
            raise ValueError(f'Prompt fingerprint mismatch: {name}')
        print(name, digest)

class Offline:
    def __init__(self, fixture):
        self.fixture = fixture

    def response(self, stage, prompt, data):
        item = self.fixture['responses'][stage]
        family = 'slots' if stage.startswith('slots') else 'answer' if stage == 'answer' else 'react'
        if prompt != getattr(engine, family if family != 'slots' else 'slot'):
            raise ValueError('Incorrect stage prompt')
        for key, expected in item.get('requires', {}).items():
            if data.get(key) != expected:
                raise ValueError(f'{stage}: request field {key} differs from fixture')
        return item['response']

    async def embed(self, parts, task):
        if task != 'event_query':
            raise ValueError(task)
        return SimpleNamespace(indexed_vectors=[self.fixture['query_vectors'][q] for q in parts])

def prepare_example(folder, fixture):
    chunks = []
    blocks = []
    vectors = []
    catalog = []
    with sqlite3.connect(folder/'meta.sqlite') as db:
        db.execute('create table chunks(id integer primary key,date text)')
        for i, item in enumerate(fixture['documents']):
            sid = item['id']
            catalog.append({'id':sid})
            chunks.append(dict(sid=sid,date=item['date'],start=0,end=0,ids=[sid]))
            blocks.append([i,item['text']])
            vectors.append(item['vector'])
            db.execute('insert into chunks values (?,?)',(i,item['date']))
            save_json(folder/'sources'/sid/'source.json',dict(name='示例日 10-00',segments=[
                dict(start_ms=0,text=item['text'],speaker_local_id='示例')]))
    save_json(folder/'sources.json',catalog)
    save_json(folder/'chunks.json',chunks)
    save_json(folder/'blocks.json',blocks)
    np.save(folder/'vectors-0.npy',np.asarray(vectors,dtype=float))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,default=ROOT/'example.json')
    parser.add_argument('--index',type=Path)
    parser.add_argument('--sources',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if bool(args.index) != bool(args.sources):
        parser.error('--index and --sources must be supplied together')
    verify_prompts()
    fixture=read_json(args.fixture)
    with tempfile.TemporaryDirectory() as tmp:
        base=Path(tmp)
        if not args.index:
            prepare_example(base,fixture)
        index.configure(args.index or base,args.sources or base)
        out=args.output or base/'out'
        if out.exists() and any(out.iterdir()):
            parser.error('--output must be empty for a fresh replay')
        out.mkdir(parents=True,exist_ok=True)
        runner=Offline(fixture)
        asyncio.run(engine.run(fixture['card'],runner,runner,out))
        row=out/f"r{fixture['card']['sheet_row']}"
        answer=read_json(row/'result.json')['answer']['answer']
        if answer!=fixture['expected_answer'] or read_json(row/'selection1.json')!=fixture['expected_selection']:
            raise ValueError('Unexpected answer or retrieval order')
        print('selection:',fixture['expected_selection'],'answer:',answer)

if __name__=='__main__':
    main()
