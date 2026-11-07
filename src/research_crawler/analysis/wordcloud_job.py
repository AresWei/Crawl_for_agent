"""MediaCrawler words.py algorithm adapted to one owned, cancellable worker.

Source: NanmiCoder/MediaCrawler 54a3d924 tools/words.py, copyright relakkes.
NON-COMMERCIAL LEARNING LICENSE 1.1. Uses per-process tokenizer, no global config
or skip-on-busy plotting lock; source observations and hashes remain explicit.
"""
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys

from ..core.records import Record
from ..core.sessions import controlled


async def generate(context):
    from ..adapters.comment_reports import _latest_rows, _evidence
    source_id = context.params.get('source_job_id')
    source = context.store.job(source_id)
    if source['status'] in {'pending', 'running'}:
        raise ValueError('Wait for source collection to settle')
    maximum = context.params.get('max_comments', 1000)
    if type(maximum) is not int or not 1 <= maximum <= 10000:
        raise ValueError('max_comments must be 1..10000')
    folder = context.paths.run(context.job_id) / 'wordcloud'
    folder.mkdir(exist_ok=True)
    checkpoint = await context.checkpoint('wordcloud')
    if checkpoint:
        selected = checkpoint['metadata']['sources']
    else:
        rows = await asyncio.to_thread(_latest_rows, context.store, source_id, 'comment')
        selected = [_evidence(row, 10000) for row in rows[:maximum]]
        if not selected or sum(len(item['text']) for item in selected) > 2_000_000:
            raise ValueError('Select 1..10000 comments within 2000000 characters')
        checkpoint = await context.commit_page('wordcloud', [], next_cursor=0, exhausted=False,
            metadata={'sources': selected, 'selection_complete':len(selected)==len(rows),
                      'source_complete':source['status']=='complete' and source['summary'].get('coverage_complete') is True})
    config = {'texts':[item['text'] for item in selected], 'stop_words':context.params.get('stop_words', []),
              'custom_words':context.params.get('custom_words', []), 'font_path':context.params.get('font_path'),
              'root':str(context.paths.root), 'output':str(folder)}
    for key in ('stop_words', 'custom_words'):
        if not isinstance(config[key], list) or len(config[key]) > 10000 or any(not isinstance(s, str) or len(s)>100 for s in config[key]):
            raise ValueError('Invalid tokenizer word list')
    request = folder / 'input.json'
    request.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(sys.executable, '-m', __name__, str(request),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        **({'creationflags':0x08000000} if os.name=='nt' else {})))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await asyncio.shield(spawn)
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    async def close():
        if process.returncode is None:
            process.kill()
        await process.wait()
    context.resources.push_async_callback(close)
    try:
        if await controlled(context, process.wait()) != 0:
            raise ValueError('Wordcloud worker failed; verify font and dependencies')
    finally:
        await close()
    files = []
    for name, mime in [('frequencies.json','application/json'), ('wordcloud.png','image/png')]:
        path = folder / name
        if path.exists():
            files.append({'path':str(path), 'bytes':path.stat().st_size, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'content_type':mime})
    counts = json.loads((folder / 'frequencies.json').read_text(encoding='utf-8'))
    complete = (checkpoint['metadata'].get('source_complete',False)
        and checkpoint['metadata'].get('selection_complete',False) and not any(item['text_truncated'] for item in selected))
    payload = {'files':files, 'sources':selected, 'sample_comments':len(selected), 'frequencies':counts,
               'coverage_complete':complete, 'model_used':False, 'empty_vocabulary':not bool(counts),
               'method':'jieba_lcut_counter', 'source_job_id':source_id}
    await context.commit_page('wordcloud', [Record('comment_analysis','wordcloud',context.job_id,'',payload)],
        next_cursor=None, exhausted=True, expected_revision=checkpoint['revision'], metadata=checkpoint['metadata'])
    return {'status':'complete' if complete else 'partial', **payload}


def worker(path):
    import jieba
    from wordcloud import WordCloud
    cfg=json.loads(Path(path).read_text(encoding='utf-8'))
    tokenizer=jieba.Tokenizer()
    tokenizer.tmp_dir=str(Path(cfg['root'])/'cache')
    for word in cfg['custom_words']:
        tokenizer.add_word(word)
    stops=set(Path(__file__).with_name('wordcloud-stopwords.txt').read_text(encoding='utf-8').splitlines()) | set(cfg['stop_words'])
    counts=Counter(word for word in tokenizer.lcut(' '.join(cfg['texts'])) if word not in stops and word.strip())
    folder=Path(cfg['output'])
    target=folder/'frequencies.json'
    temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(counts.most_common()),ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,target)
    if counts:
        font=cfg['font_path'] or (r'C:\Windows\Fonts\msyh.ttc' if os.name=='nt' else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        cloud=WordCloud(font_path=font,width=800,height=400,background_color='white',max_words=200,
            stopwords=stops,colormap='viridis',contour_color='steelblue',contour_width=1,random_state=42)
        cloud.generate_from_frequencies(dict(counts.most_common(20)))
        cloud.to_image().save(folder/'wordcloud.tmp',format='PNG')
        os.replace(folder/'wordcloud.tmp',folder/'wordcloud.png')


if __name__ == '__main__':
    worker(sys.argv[1])
