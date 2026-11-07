"""Explicit Crawl4AI cache transfer through the shared job/store lifecycle.

Schema and field layout adapted from frozen async_database.py and legacy/database.py
at 838869bdda93f25e6d50b3f50ae9577d9b19a586. Apache-2.0/NOTICE retained.
No original manager is imported; imports never populate anonymous PageCache.
"""
from __future__ import annotations
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from ..core.records import Record, canonical_json, utcnow

CONTENT={'html':'html_content','cleaned_html':'cleaned_html','markdown':'markdown_content',
         'extracted_content':'extracted_content','screenshot':'screenshots'}
JSON_FIELDS=('media','links','metadata','response_headers','downloaded_files')
COLUMNS=('url','html','cleaned_html','markdown','extracted_content','success','media','links','metadata',
         'screenshot','response_headers','downloaded_files','etag','last_modified','head_fingerprint','cached_at')
SCHEMA='CREATE TABLE crawled_data ('+', '.join(name+(' TEXT PRIMARY KEY' if name=='url' else ' REAL' if name=='cached_at' else ' BOOLEAN' if name=='success' else ' TEXT') for name in COLUMNS)+')'

def sha(data):return hashlib.sha256(data).hexdigest()

def _bound(params,name,default,maximum):
    value=params.get(name,default)
    if type(value) is not int or not 1<=value<=maximum:raise ValueError('Invalid '+name)
    return value

def _read(path,maximum):
    if path.stat().st_size>maximum:raise ValueError('cache_file_byte_limit')
    return path.read_bytes()

def _fingerprint(path):
    values=[]
    for file in (path,Path(str(path)+'-wal')):
        if file.exists():values.append([file.name,sha(_read(file,128_000_000))])
    return sha(canonical_json(values).encode())

def _rows(path,after,limit,failed):
    # mode=ro prevents modifying the original cache and does not execute its schema.
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
        columns={row[1] for row in db.execute('PRAGMA table_info(crawled_data)')}
        if not {'url','html','markdown','success'}<=columns:raise ValueError('Unknown Crawl4AI cache schema')
        if failed:
            values=[dict(row) for row in db.execute('SELECT * FROM crawled_data WHERE url IN ('+','.join('?' for _ in failed)+') ORDER BY url',failed)]
        else:values=[dict(row) for row in db.execute('SELECT * FROM crawled_data WHERE url>? ORDER BY url LIMIT ?',(after,limit+1))]
    return values[:limit],len(values)>limit

def _headers(value):
    if not isinstance(value,dict):return {}
    return {key:item for key,item in value.items() if str(key).lower() not in {'set-cookie','cookie','authorization','proxy-authorization','x-api-key'}}

def decode_row(row,root,format,maximum,allow_gzip=False):
    output=dict(row);errors=[];blob_evidence={}
    for field,directory in CONTENT.items():
        value=row.get(field) or ''
        if not isinstance(value,str):errors.append(field+'_invalid');value=''
        if format=='files' and value:
            if not re.fullmatch(r'[0-9a-fA-F]{16}|[0-9a-fA-F]{64}',value):
                errors.append(field+'_invalid_reference');value=''
            else:
                path=(root/directory/value).resolve()
                if not path.is_relative_to(root):raise ValueError('Content path escapes selected root')
                try:
                    raw=_read(path,maximum);compression='none'
                    if raw.startswith(b'\x1f\x8b'):
                        if not allow_gzip:raise ValueError('gzip_extension_not_enabled')
                        with gzip.open(path,'rb') as stream:raw=stream.read(maximum+1)
                        if len(raw)>maximum:raise ValueError('decompressed_byte_limit')
                        compression='explicit_gzip_extension'
                    text=raw.decode('utf-8')
                    blob_evidence[field]={'reference':value,'sha256':sha(raw),'bytes':len(raw),'compression':compression,
                                          'upstream_xxh64_verified':False}
                    value=text
                except (OSError,ValueError,UnicodeError):errors.append(field+'_blob_unavailable_or_invalid');value=''
        if len(value.encode())>maximum:errors.append(field+'_byte_limit');value=''
        output[field]=value
    for field in JSON_FIELDS:
        value=row.get(field)
        if isinstance(value,str):
            try:value=json.loads(value) if value else ([] if field=='downloaded_files' else {})
            except ValueError:errors.append(field+'_invalid_json');value={}
        output[field]=value if isinstance(value,(list,dict)) else {}
    output['response_headers']=_headers(output['response_headers'])
    markdown=output['markdown']
    try:parsed=json.loads(markdown)
    except (ValueError,TypeError):parsed=None
    if isinstance(parsed,dict):
        output['markdown_result']=parsed;output['markdown']=parsed.get('raw_markdown','')
        if not isinstance(output['markdown'],str):errors.append('markdown_invalid');output['markdown']=''
    if not output['html']:errors.append('html_missing')
    output['legacy_blob_evidence']=blob_evidence
    return output,errors


async def import_cache(ctx):
    p=ctx.params
    database=Path(p.get('database','')).expanduser().resolve(strict=True)
    if not database.is_file():raise ValueError('Explicit SQLite database required')
    identity=p.get('identity_scope')
    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',identity):
        raise ValueError('Explicit identity_scope label required; cache identity cannot be inferred')
    format=p.get('format','files')
    if format not in {'files','inline'}:raise ValueError('Choose files or inline cache format explicitly')
    root=Path(p.get('content_root',database.parent)).expanduser().resolve(strict=True)
    count=_bound(p,'max_records',100,1000);maximum=_bound(p,'max_record_bytes',4_000_000,16_000_000)
    fingerprint=await asyncio.to_thread(_fingerprint,database)
    partition=sha(canonical_json([str(database),identity,format,str(root)]).encode())
    checkpoint=await ctx.checkpoint('legacy_cache_import');revision=checkpoint['revision'] if checkpoint else 0
    state=checkpoint['cursor'] if checkpoint else {'fingerprint':fingerprint,'partition':partition,'after':'','failed':[],'imported':0,'imported_at':utcnow()}
    if state['fingerprint']!=fingerprint or state['partition']!=partition:raise ValueError('Selected cache changed; create a new import job')
    if checkpoint and checkpoint['exhausted']:return {'status':'complete','imported':state['imported'],'resumed':True}
    retry=list(state['failed'])
    rows,more=await asyncio.to_thread(_rows,database,state['after'],count,retry)
    if await asyncio.to_thread(_fingerprint,database)!=fingerprint:raise ValueError('Cache changed during read')
    if checkpoint is None:
        checkpoint=await ctx.commit_page('legacy_cache_import',[],next_cursor=state,exhausted=False,expected_revision=revision);revision=checkpoint['revision']
    for row in rows:
        await ctx.check_control()
        original,errors=await asyncio.to_thread(decode_row,row,root,format,maximum,p.get('allow_gzip',False))
        if len(canonical_json(original).encode())>maximum:raise ValueError('Combined cache row exceeds byte limit')
        cached_at=row.get('cached_at');source_time=None
        if isinstance(cached_at,(int,float)) and 0<cached_at<32503680000:
            source_time=datetime.fromtimestamp(cached_at,timezone.utc).isoformat()
        payload={**original,'final_url':row['url'],'source_fetched_at':None,
            'cache_saved_at':source_time,'capture_time_known':False,
            'legacy_cache':{'format':format,'identity_scope':identity,'partition':partition,'database_sha256':fingerprint,
                'original_database':str(database),'imported_at':state['imported_at'],'cached_at_is_capture_time':False,
                'automatically_reusable_page_cache':False,'errors':errors}}
        record=Record(source='legacy_cache',kind='page',native_id=partition+':'+row['url'],url=row['url'],payload=payload,
            observed_at=state['imported_at'],coverage={'complete':not errors and bool(row['success']),'scope':'selected_archived_cache_record','live_fetch':False})
        if row['url'] in state['failed']:state['failed'].remove(row['url'])
        if errors or not row['success']:state['failed'].append(row['url'])
        if not retry:state['after']=row['url'];state['imported']+=1
        checkpoint=await ctx.commit_page('legacy_cache_import',[record],next_cursor=state,exhausted=False,expected_revision=revision);revision=checkpoint['revision']
    # Retry only failed rows in this invocation; remaining new rows stay queued by cursor.
    if retry:
        pending,_=await asyncio.to_thread(_rows,database,state['after'],1,[])
        more=bool(pending)
    complete=not more and not state['failed']
    await ctx.commit_page('legacy_cache_import',[],next_cursor=state,exhausted=complete,expected_revision=revision)
    return {'status':'complete' if complete else 'partial','imported':state['imported'],'failed':len(state['failed']),
            'reason':None if complete else 'cache_records_incomplete' if state['failed'] else 'record_budget',
            'identity_scope':identity,'partition':partition,'live_fetch':False,'page_cache_promoted':False}


def _build_export(directory,rows,job_id,max_bytes):
    owner=directory/'.owner.json'
    if directory.exists() and not owner.exists():raise ValueError('Export directory is not job-owned')
    directory.mkdir(parents=True,exist_ok=True)
    if owner.exists() and json.loads(owner.read_text())!={'job_id':job_id}:raise ValueError('Export belongs to a different job')
    owner.write_text(json.dumps({'job_id':job_id}),encoding='utf-8')
    sidecar_bytes=canonical_json({'job_id':job_id,'observations':rows,
        'url_projection':'latest selected observation per URL; all selected versions retained here',
        'hash_names':'SHA256; upstream loader treats filename as opaque reference'}).encode()
    selected={};total=len(sidecar_bytes);receipts=[]
    if total>max_bytes:raise ValueError('Export sidecar byte budget')
    for row in rows:selected[row['url']]=row
    database=directory/'crawl4ai.db';temporary=directory/'crawl4ai.db.partial'
    if temporary.exists():temporary.unlink()
    with closing(sqlite3.connect(temporary)) as db:
        db.execute(SCHEMA)
        for url,row in selected.items():
            payload=row['payload'];values={'url':url,'success':int(row['coverage'].get('complete',False))}
            for field,folder in CONTENT.items():
                content=payload.get(field,'') or ''
                if field=='markdown':content=canonical_json(payload.get('markdown_result',{'raw_markdown':content}))
                if not isinstance(content,str):content=canonical_json(content)
                encoded=content.encode();total+=len(encoded)
                if total>max_bytes:raise ValueError('Export byte budget')
                reference=sha(encoded) if content else '';values[field]=reference
                if reference:
                    file=directory/folder/reference;file.parent.mkdir(exist_ok=True);file.write_bytes(encoded)
                    receipts.append({'path':str(file),'sha256':reference,'bytes':len(encoded)})
            for field in JSON_FIELDS:
                value=payload.get(field,[] if field=='downloaded_files' else {})
                if field=='response_headers':value=_headers(value)
                if field=='links' and isinstance(value,list):
                    value={'internal':[dict(link,href=link.get('url',link.get('href',''))) for link in value if isinstance(link,dict)],'external':[]}
                values[field]=canonical_json(value)
            for field in ('etag','last_modified','head_fingerprint'):values[field]=payload.get(field,'')
            # Unknown capture/cache time remains zero, never replaced by export time.
            values['cached_at']=payload.get('cached_at') or 0
            db.execute('INSERT INTO crawled_data VALUES ('+','.join('?' for _ in COLUMNS)+')',[values.get(k,'') for k in COLUMNS])
        db.commit()
    if total+temporary.stat().st_size>max_bytes:raise ValueError('Export aggregate byte budget')
    temporary.replace(database)
    sidecar=directory/'provenance.json';sidecar.write_bytes(sidecar_bytes)
    receipts.extend({'path':str(p),'sha256':sha(p.read_bytes()),'bytes':p.stat().st_size} for p in (database,sidecar))
    return {'database':str(database),'directory':str(directory),'rows':len(selected),'observations':len(rows),'files':receipts}


def _clean_owned_stage(directory,parent,job_id):
    """Delete only a verified export-owned tree; never follow an escaped link."""
    if not directory.exists():return
    resolved=directory.resolve()
    if resolved.parent!=parent.resolve():raise ValueError('Export cleanup escaped selected parent')
    owner=resolved/'.owner.json'
    if not owner.is_file() or json.loads(owner.read_text())!={'job_id':job_id}:
        raise ValueError('Export cleanup ownership mismatch')
    children=list(resolved.rglob('*'))
    # Validate the entire list before deleting even one file. Unexpected user files
    # are retained, including in a previous successful output backup.
    known_root={'.owner.json','crawl4ai.db','crawl4ai.db.partial','provenance.json'}
    for child in children:
        actual=child.resolve()
        if not actual.is_relative_to(resolved) or child.is_symlink():
            raise ValueError('Export cleanup cannot follow external links')
        relative=child.relative_to(resolved)
        valid=(len(relative.parts)==1 and relative.name in known_root and child.is_file()
               or len(relative.parts)==1 and relative.name in CONTENT.values() and child.is_dir()
               or len(relative.parts)==2 and relative.parts[0] in CONTENT.values()
                  and re.fullmatch(r'[0-9a-f]{64}',relative.name) and child.is_file())
        if not valid:raise ValueError('Export cleanup found an unowned extra file')
    for child in sorted(children,key=lambda path:len(path.parts),reverse=True):
        if child.is_dir():child.rmdir()
        else:child.unlink()
    resolved.rmdir()


def _write_export(directory,rows,job_id,max_bytes):
    """Build in an owned sibling stage; budget/SQLite failure cannot alter prior output."""
    directory=Path(directory).resolve();parent=directory.parent
    if directory.exists():
        owner=directory/'.owner.json'
        if not owner.is_file() or json.loads(owner.read_text())!={'job_id':job_id}:
            raise ValueError('Export directory is not job-owned')
    parent.mkdir(parents=True,exist_ok=True)
    suffix=uuid.uuid4().hex
    stage=parent/(directory.name+'.'+suffix+'.staging')
    previous=parent/(directory.name+'.'+suffix+'.previous')
    stage.mkdir();(stage/'.owner.json').write_text(json.dumps({'job_id':job_id}),encoding='utf-8')
    moved=False
    try:
        receipt=_build_export(stage,rows,job_id,max_bytes)
        # Validate all receipt paths before promotion; none may reference outside stage.
        for item in receipt['files']:
            if not Path(item['path']).resolve().is_relative_to(stage):raise ValueError('Invalid staged export receipt')
        if directory.exists():directory.replace(previous);moved=True
        try:stage.replace(directory)
        except BaseException:
            if moved:previous.replace(directory);moved=False
            raise
        receipt['database']=str(directory/'crawl4ai.db');receipt['directory']=str(directory)
        for item in receipt['files']:item['path']=str(directory/Path(item['path']).relative_to(stage))
        if moved:
            try:_clean_owned_stage(previous,parent,job_id)
            except (OSError,ValueError):receipt['retained_previous_directory']=str(previous)
        return receipt
    finally:
        if stage.exists():_clean_owned_stage(stage,parent,job_id)


def _selected_rows(store,source,count,maximum):
    job=store.job(source)
    if job['status'] in {'pending','running'}:raise ValueError('Export requires a settled source job')
    rows=[];size=0;limited=False
    while len(rows)<count:
        batch=store.observations(source,limit=min(5,count-len(rows)),offset=len(rows))
        if not batch:break
        for row in batch:
            size+=len(canonical_json(row).encode())
            if size>maximum:limited=True;break
            rows.append(row)
        if limited:break
    if store.job(source)['updated_at']!=job['updated_at']:raise ValueError('Source changed during export selection')
    return rows,limited


async def export_cache(ctx):
    source=ctx.params.get('source_job_id')
    if not isinstance(source,str) or not source:raise ValueError('source_job_id required')
    count=_bound(ctx.params,'max_records',1000,1000);maximum=_bound(ctx.params,'max_bytes',64_000_000,256_000_000)
    checkpoint=await ctx.checkpoint('legacy_cache_export');revision=checkpoint['revision'] if checkpoint else 0
    rows,byte_limited=await asyncio.to_thread(_selected_rows,ctx.store,source,count,maximum)
    if not rows:raise ValueError('Selected source job has no records')
    if any(row['kind']!='page' or row['source'] not in {'web','legacy_cache'} for row in rows):raise ValueError('Select a web/cache source job')
    identity=sha(canonical_json([[r['record_key'],r['content_hash']] for r in rows]).encode())
    if checkpoint and checkpoint['cursor']['source_identity']!=identity:raise ValueError('Source observation selection changed')
    if checkpoint and checkpoint['exhausted']:
        receipt=checkpoint['cursor']['receipt']
        if all(Path(item['path']).exists() and sha(Path(item['path']).read_bytes())==item['sha256'] for item in receipt['files']):
            return {'status':'complete',**receipt,'resumed':True}
    directory=ctx.paths.run(ctx.job_id)/'crawl4ai-cache'
    task=asyncio.create_task(asyncio.to_thread(_write_export,directory,rows,ctx.job_id,maximum))
    try:receipt=await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:await asyncio.shield(task)
            except asyncio.CancelledError:pass
        task.result();raise
    await ctx.check_control()
    more=byte_limited or (await asyncio.to_thread(ctx.store.counts,source))['versions']>len(rows)
    await ctx.commit_page('legacy_cache_export',[],next_cursor={'source_identity':identity,'receipt':receipt},
                         exhausted=not more,expected_revision=revision)
    return {'status':'partial' if more else 'complete',**receipt,'reason':('source_byte_budget' if byte_limited else 'record_budget') if more else None,
            'source_capture_time_preserved':True,'live_fetch':False}
