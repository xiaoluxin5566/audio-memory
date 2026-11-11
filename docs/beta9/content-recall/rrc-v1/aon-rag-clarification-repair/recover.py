import asyncio,gzip,json,shutil
from pathlib import Path
import batch as b

def normalize_scope(value):
    if isinstance(value,str):
        dates=b.re.findall(r'\d{4}-\d{2}-\d{2}',value)
        if not dates:
            dates=['2026-'+v for v in b.re.findall(r'(?<![0-9-])\d{2}-\d{2}(?![0-9-])',value)]
        if len(dates)==2:return dict(start_date=dates[0],end_date=dates[1])
    return value

# Normalize representations without changing the range chosen by the model.
original_run=b.run
source=(b.HERE/'batch.py').read_text()
source=source[source.index('async def run('):source.index('def batch_identity(')]
source=source.replace("        plans.append(p);return p","""        if isinstance(p.get('content'),dict):p=p['content']
        for key in ('initial_scope','fallback_scope'):
            p[key]=normalize_scope(p.get(key))
        if isinstance(p.get('initial_scope'),dict):
            p={**p['initial_scope'],**p}
        plans.append(p);return p""")
# exec in owning module globals so OUT and caches remain shared.
b.normalize_scope=normalize_scope
exec(source,b.__dict__)

async def main():
    b.OUT=b.HERE/'replay';b.c.R=b.OUT
    original=next((b.HERE.parent/'aon-rag-baseline-1/runs').iterdir())
    cards={c['sheet_row']:c for c in b.c.read(b.HERE/'cards.json')['cards']}
    failures={int(f.parent.name[1:]) for f in b.OUT.glob('r*/failure.json')}
    failures.update(int(f.parent.name[1:]) for f in original.glob('r*/failure.json'))
    async with b.ReceiptHTTP(trust_env=False,follow_redirects=False) as http:
        chat,enc=b.c.clients(http)
        for n in sorted(failures):
            label=f'r{n}';folder=b.OUT/label
            if not folder.exists():
                shutil.copytree(original/label,folder)
                if (original/'receipts'/label).exists():shutil.copytree(original/'receipts'/label,b.OUT/'receipts'/label)
            if (folder/'result.json').exists():continue
            for response in (b.OUT/'receipts'/label).glob('*/*.response.json.gz'):
                data=json.loads(gzip.decompress(response.read_bytes()))
                try:b.pilot.parse_model_object(data['choices'][0]['message']['content'])
                except (ValueError,KeyError,TypeError):
                    response.rename(response.with_name(response.name+'.invalid'))
            try:
                await b.run(cards[n],http,enc,chat.token)
                (folder/'failure.json').unlink(missing_ok=True)
            except Exception as exc:b.c.save(folder/'failure.json',dict(error=type(exc).__name__,detail=str(exc)))

if __name__=='__main__':asyncio.run(main())
