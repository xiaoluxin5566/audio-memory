import asyncio,json,shutil,time
from pathlib import Path
import batch as b

async def main():
    b.OUT=b.HERE/'replay'
    b.OUT.mkdir(exist_ok=True)
    b.c.R=b.OUT
    authorization=b.HERE/'authorization.json'
    if not authorization.exists():
        raise RuntimeError('new replay budget awaiting approval')
    b.c.save(b.OUT/'authorization.json',b.c.read(authorization))
    original=next((b.HERE.parent/'aon-rag-baseline-1/runs').iterdir())
    rows=set(b.c.read(b.HERE/'rows.json'))
    cards=[c for c in b.c.read(b.HERE/'cards.json')['cards'] if c['sheet_row'] in rows]
    async with b.ReceiptHTTP(trust_env=False,follow_redirects=False) as http:
        chat,enc=b.c.clients(http)
        for card in cards:
            label=f"r{card['sheet_row']}"
            if not (b.OUT/label).exists():
                shutil.copytree(b.HERE/'seed'/label,b.OUT/label)
                source=original/'receipts'/label
                if source.exists():shutil.copytree(source,b.OUT/'receipts'/label)
            try:await b.run(card,http,enc,chat.token)
            except Exception as exc:
                b.c.save(b.OUT/label/'failure.json',dict(error=type(exc).__name__,detail=str(exc)))
                if 'budget' in str(exc):break

if __name__=='__main__':asyncio.run(main())
