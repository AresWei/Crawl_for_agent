"""Bounded local adaptation of Crawl4AI LLMTableExtraction.

Original chunk wrappers, shape validation and ordered merge are preserved in
local_table_algorithms.py. Model interpretation stays separate from source tables.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib
import time
from lxml import etree, html
from .local_extraction import extract_local, _integer, _number
from .local_table_algorithms import TableAlgorithms
from .table_extraction import DefaultTableExtraction

SCHEMA={'type':'object','properties':{
    'headers':{'type':'array','items':{'type':'string'}},
    'rows':{'type':'array','items':{'type':'array','items':{'type':'string'}}}},
    'required':['headers','rows'],'additionalProperties':False}
INSTRUCTION=('Convert this HTML table into one table record. data must contain headers and rows, both strings only. '
    'Duplicate rowspan/colspan values across occupied positions. Keep every body and footer row in source order. '
    'Do not invent or abbreviate values. Use exact cell text, preserve numbers and formatting. '
    'Keep headers separate from body rows. evidence must quote exact text present in the HTML. '
    'For multiple header rows, headers contains the deepest nonempty header for each column, expanding spans; '
    'do not concatenate parent and child header names. Only extract rows belonging to the outermost table. '
    'A nested table is not an additional outer row: its text remains in its containing outer cell, '
    'in DOM order separated by spaces. Nested tables are extracted independently by the caller. '
    'Return exactly one record, even for a headerless table; use empty headers then.')

def digest(text):return hashlib.sha256(text.encode()).hexdigest()
def serialize(element):return etree.tostring(element,encoding='unicode',with_tail=False)
def own_rows(table):return [row for row in table.xpath('.//tr') if next(row.iterancestors('table'),None) is table]
def cell_text(cell):return ' '.join(' '.join(cell.itertext()).split())

def row_sections(table):
    headers=[];body=[]
    for row in own_rows(table):
        cells=row.xpath('./th|./td')
        implicit=bool(cells) and all(cell.tag=='th' and cell.get('scope')!='row' for cell in cells)
        group=next((node.tag for node in row.iterancestors() if node.tag in {'thead','tbody','tfoot','table'}),None)
        if group=='thead' or (not body and implicit):headers.append(row)
        else:body.append(row)
    return headers,body

def source_position_grid(rows):
    """Reuse upstream span placement, with source XPath tokens instead of text.

    Copies protect source DOM; rowspan=0 explicitly reaches the remaining group.
    The returned positions bind duplicate text to its actual source row/column.
    """
    copies=[]
    for index,row in enumerate(rows):
        copy=deepcopy(row)
        for original,cell in zip(row.xpath('./td|./th'),copy.xpath('./td|./th')):
            for child in list(cell):cell.remove(child)
            cell.text=original.getroottree().getpath(original)
            if cell.get('rowspan')=='0':cell.set('rowspan',str(row_group_remaining(rows,index)))
        copies.append(copy)
    return DefaultTableExtraction._build_grid(copies)


def row_group_remaining(rows,index):
    parent=rows[index].getparent()
    return next((n for n in range(1,len(rows)-index) if rows[index+n].getparent() is not parent),len(rows)-index)


def serialized_group_row(rows,index):
    # Chunk wrappers merge tbody elements. Resolve rowspan=0 before serialization
    # so its original row-group extent cannot silently expand in the fragment.
    row=deepcopy(rows[index])
    for cell in row.xpath('./th|./td'):
        if cell.get('rowspan')=='0':cell.set('rowspan',str(row_group_remaining(rows,index)))
    return serialize(row)


def source_structure(table):
    """DOM geometry, not an inferred semantic table: all paths address source cells."""
    header_nodes,body_nodes=row_sections(table)
    header_positions=source_position_grid(header_nodes)
    body_positions=source_position_grid(body_nodes)
    cells={cell.getroottree().getpath(cell):cell for row in own_rows(table) for cell in row.xpath('./th|./td')}
    width=max((len(row) for row in header_positions+body_positions),default=0)
    header_rows=[[cell_text(cells[path]) if path else '' for path in row]+['']*(width-len(row)) for row in header_positions]
    paths=[];path_refs=[];headers=[]
    for column in range(width):
        seen=set();names=[];refs=[]
        for level,row in enumerate(header_positions):
            path=row[column] if column<len(row) else ''
            if not path or path in seen:continue
            seen.add(path);value=cell_text(cells[path])
            names.append(value);refs.append({'level':level,'column':column,'value':value,'source_xpath':path})
        paths.append(names);path_refs.append(refs)
        if header_positions:headers.append(next((value for value in reversed(names) if value),''))
    return {'headers':headers,'header_rows':header_rows,'header_paths':paths,'header_path_refs':path_refs,
            'header_positions':header_positions,'body_positions':body_positions,'cells':cells}


def original_cell_refs(table,fragment,refs,body_start,row_count):
    """Bind fragment coordinates back to original HTML DOM; reject mismatched copies."""
    fragment_table=html.fromstring(fragment)
    fh,fb=row_sections(fragment_table);sh,sb=row_sections(table)
    original_rows=sh+sb[body_start:body_start+row_count]
    fragment_rows=fh+fb
    if len(original_rows)!=len(fragment_rows):raise ValueError('original_source_row_mapping_mismatch')
    mapping={}
    for original_row,fragment_row in zip(original_rows,fragment_rows):
        originals=original_row.xpath('./th|./td');copies=fragment_row.xpath('./th|./td')
        if len(originals)!=len(copies):raise ValueError('original_source_column_mapping_mismatch')
        for original,copy in zip(originals,copies):
            if cell_text(original)!=cell_text(copy):raise ValueError('original_source_text_mismatch')
            mapping[copy.getroottree().getpath(copy)]=original
    for ref in refs:
        paths=ref['source_cell_candidates']
        if not paths:ref['source_original']=None;continue
        original=mapping[paths[0]]
        if cell_text(original)!=ref['value']:raise ValueError('original_source_value_mismatch')
        ref['source_original']={'xpath':original.getroottree().getpath(original),
            'value':ref['value'],'html_sha256':digest(serialize(original)),
            'normalization':'DOM itertext with Unicode whitespace collapsed',
            'includes_nested_table_text':bool(original.xpath('.//table'))}
    return refs


def strict_source_grid(table,*,require_headers=True,allow_padding=False):
    """Allow DOM recovery only for explicit, rectangular, nonoverlapping geometry."""
    header_rows,body_rows=row_sections(table)
    if require_headers and not header_rows:raise ValueError('source_grid_requires_explicit_headers')
    grids=[]
    for rows in (header_rows,body_rows):
        grid=[[] for _ in rows]
        for r,row in enumerate(rows):
            column=0
            for cell in row.xpath('./th|./td'):
                try:height=int(cell.get('rowspan','1'));width=int(cell.get('colspan','1'))
                except ValueError:raise ValueError('source_grid_invalid_span') from None
                if height==0:
                    height=row_group_remaining(rows,r)
                if height<1 or not 1<=width<=DefaultTableExtraction.COLSPAN_LIMIT or r+height>len(rows):
                    raise ValueError('source_grid_invalid_span')
                if any(rows[n].getparent() is not row.getparent() for n in range(r,r+height)):
                    raise ValueError('source_grid_crosses_row_group')
                while column<len(grid[r]) and grid[r][column] is not None:column+=1
                path=cell.getroottree().getpath(cell)
                for target_row in range(r,r+height):
                    grid[target_row].extend([None]*max(0,column+width-len(grid[target_row])))
                    for target_column in range(column,column+width):
                        if grid[target_row][target_column] is not None:raise ValueError('source_grid_overlap')
                        grid[target_row][target_column]=path
                column+=width
        grids.append(grid)
    all_rows=grids[0]+grids[1]
    widths={len(row) for row in all_rows}
    if not allow_padding and (len(widths)!=1 or not next(iter(widths),0) or any(path is None for row in all_rows for path in row)):
        raise ValueError('source_grid_missing_cells')
    structure=source_structure(table)
    comparable=[[[path or '' for path in row] for row in grid if row] for grid in grids]
    if comparable!=[structure['header_positions'],structure['body_positions']]:
        raise ValueError('source_grid_geometry_disagreement')
    return {'headers':structure['headers'],
            'rows':[[cell_text(structure['cells'][path]) if path else '' for path in row] for row in grids[1]]}

def table_chunks(table,max_chars,enable_chunking):
    """Repeat headers; never cut a connected rowspan group between calls."""
    headers,body=row_sections(table)
    if not enable_chunking:return [(serialize(table),len(body))]
    head='<thead>'+''.join(serialized_group_row(headers,index) for index in range(len(headers)))+'</thead>' if headers else ''
    algorithm=TableAlgorithms();groups=[];current=[];until=-1
    for index,row in enumerate(body):
        current.append(serialized_group_row(body,index))
        for cell in row.xpath('./td|./th'):
            try:span=int(cell.get('rowspan','1'))
            except ValueError:span=1
            until=max(until,index+row_group_remaining(body,index)-1 if span==0 else min(len(body)-1,index+max(1,span)-1))
        if index>=until:groups.append(current);current=[]
    if current:groups.append(current)
    chunks=[];pending=[]
    for group in groups:
        candidate=algorithm._create_chunk_html(head,pending+group,None)
        if pending and len(candidate)>max_chars:
            chunks.append((algorithm._create_chunk_html(head,pending,None),len(pending)));pending=[]
        pending.extend(group)
    if pending or not groups:chunks.append((algorithm._create_chunk_html(head,pending,None),len(pending)))
    return chunks

def verify_table(data,fragment,expected_rows):
    algorithm=TableAlgorithms()
    if not algorithm._validate_table_structure(data):return None,'invalid_table_shape'
    headers,rows=data['headers'],data['rows']
    if len(rows)!=expected_rows:return None,'omitted_or_extra_rows'
    width=len(headers) or max(map(len,rows),default=0)
    if any(len(row)!=width for row in rows):return None,'inconsistent_columns'
    tree=html.fromstring(fragment)
    try:strict_source_grid(tree,require_headers=False,allow_padding=True)
    except ValueError as error:return None,str(error)
    structure=source_structure(tree)
    cells=[cell for row in own_rows(tree) for cell in row.xpath('./td|./th')]
    source=[{'text':cell_text(cell),'xpath':cell.getroottree().getpath(cell)} for cell in cells]
    lookup={}
    for cell in source:lookup.setdefault(cell['text'],[]).append(cell['xpath'])
    refs=[];observed=set()
    matrices=[('headers',[headers]),('rows',rows)]
    if len(structure['header_rows'])>1:matrices.append(('header_rows',structure['header_rows']))
    for kind,matrix in matrices:
        for r,row in enumerate(matrix):
            for c,value in enumerate(row):
                if value and value not in lookup:return None,'unsupported_cell_value'
                observed.add(value)
                refs.append({'section':kind,'row':r,'column':c,'value':value,
                    'source_cell_candidates':lookup.get(value,[]),'empty_padding':not value and '' not in lookup})
    if any(cell['text'] and cell['text'] not in observed for cell in source):
        return None,'source_cells_omitted'
    # Exact values somewhere in the table do not establish row/column identity.
    # Reject permutations and preserve the original rowspan/colspan geometry.
    positions=structure['body_positions']
    header_positions=structure['header_positions']
    by_path={cell['xpath']:cell['text'] for cell in source}
    expected=[[by_path.get(path,'') for path in row] for row in positions]
    if len(expected)!=len(rows) or any(len(row)>width for row in expected):return None,'source_cell_position_mismatch'
    expected=[row+['']*(width-len(row)) for row in expected]
    expected_headers=structure['headers']
    if rows!=expected or headers!=expected_headers:return None,'source_cell_position_mismatch'
    for ref in refs:
        if ref['section']=='headers':
            path=next((row[ref['column']] for row in reversed(header_positions)
                if ref['column']<len(row) and row[ref['column']] and by_path[row[ref['column']]]==ref['value']),'')
        else:
            grid=header_positions if ref['section']=='header_rows' else positions
            paths=grid[ref['row']]
            path=paths[ref['column']] if ref['column']<len(paths) else ''
        ref['source_cell_candidates']=[path] if path else []
        ref['empty_padding']=not path
    return refs,None

async def extract_tables_local(document,url,config,*,context=None,model=None):
    if not isinstance(config,dict):raise ValueError('llm_tables must be an object')
    maximum=_integer(config,'max_tables',8,1,64)
    maximum_chunks=_integer(config,'max_chunks',16,1,128)
    chars=_integer(config,'max_chunk_chars',3500,128,4500)
    source_limit=_integer(config,'max_source_chars',300000,1,2000000)
    tries=_integer(config,'max_tries',2,1,3)
    call_limit=_integer(config,'max_calls',32,1,1000)
    token_limit=_integer(config,'max_total_tokens',32768,1,1000000)
    max_tokens=_integer(config,'max_tokens',768,1,1024)
    seconds=_number(config,'max_seconds',120,.01,7200)
    started=time.monotonic()
    calls=reserved=0
    chunking=config.get('enable_chunking',True)
    if type(chunking) is not bool:raise ValueError('enable_chunking must be boolean')
    source_fallback=config.get('source_grid_fallback',True)
    if type(source_fallback) is not bool:raise ValueError('source_grid_fallback must be boolean')
    original=document.get('html','')
    result={'status':'complete','tables':[],'attempts':[],'usage':{},'limitations':[],
        'source':{'url':url,'field':'html','sha256':digest(original),'characters':len(original)},
        'coverage':{'complete':True,'total_tables':0,'processed_tables':0,'total_chunks':0,'processed_chunks':0},
        'model_claim':True,'semantic_accuracy_automatically_proven':False}
    def partial(reason):
        result['status']='partial';result['coverage']['complete']=False
        if reason not in result['limitations']:result['limitations'].append(reason)
    if len(original)>source_limit:partial('source_character_budget');return result
    root=html.fromstring(original or '<html></html>')
    selected=root.cssselect(config['css_selector']) if config.get('css_selector') else [root]
    tables=[]
    for node in selected:
        for table in ([node] if node.tag=='table' else [])+node.xpath('.//table'):
            if table not in tables:tables.append(table)
    result['coverage']['total_tables']=len(tables)
    if len(tables)>maximum:partial('table_budget')
    used=0;algorithm=TableAlgorithms()
    for table_index,table in enumerate(tables[:maximum]):
        if time.monotonic()-started>=seconds:partial('table_time_budget');break
        if context:await context.check_control()
        try:strict_source_grid(table,require_headers=False,allow_padding=True)
        except ValueError as source_error:
            partial(str(source_error))
            result.setdefault('source_grid_failures',[]).append({'table_index':table_index,'reason':str(source_error)})
            continue
        fragments=table_chunks(table,chars,chunking);result['coverage']['total_chunks']+=len(fragments)
        valid=[];table_complete=True;body_offset=0
        complex_table=len(row_sections(table)[0])>1 or bool(table.xpath('.//table'))
        for chunk_index,(fragment,row_count) in enumerate(fragments):
            body_start=body_offset;body_offset+=row_count
            if time.monotonic()-started>=seconds:partial('table_time_budget');table_complete=False;break
            if used>=maximum_chunks:partial('chunk_budget');table_complete=False;break
            if len(fragment)>chars:partial('rowspan_group_or_table_exceeds_prompt');table_complete=False;continue
            used+=1
            options={key:config[key] for key in ('max_tokens','max_calls','max_total_tokens','max_seconds') if key in config}
            options.update(schema=SCHEMA,instruction=INSTRUCTION,input_format='html',max_chunks=1,max_records=1,
                           apply_chunking=False,max_chunk_chars=chars,max_source_chars=chars)
            accepted=None
            for attempt in range(tries):
                remaining=seconds-(time.monotonic()-started)
                if remaining<.01:
                    error='table_time_budget';partial(error);table_complete=False;break
                if calls>=call_limit or reserved>=token_limit:
                    error='table_model_budget';partial(error);table_complete=False;break
                options['max_tokens']=min(max_tokens,token_limit-reserved)
                calls+=1;reserved+=options['max_tokens']
                options['max_seconds']=remaining
                try:
                    async with asyncio.timeout(remaining):
                        extracted=await extract_local({'cleaned_html':fragment},url,options,context=context,model=model)
                except TimeoutError:
                    error='table_time_budget';partial(error);table_complete=False
                    result['attempts'].append({'table_index':table_index,'chunk_index':chunk_index,'attempt':attempt+1,
                        'status':'partial','error':error,'usage':{}})
                    break
                for key,value in extracted['usage'].items():result['usage'][key]=result['usage'].get(key,0)+value
                error='model_processing_incomplete'
                if extracted['status']=='complete' and len(extracted['records'])==1:
                    data=extracted['records'][0]['data'];references,error=verify_table(data,fragment,row_count)
                    if error is None:
                        references=original_cell_refs(table,fragment,references,body_start,row_count)
                        accepted={'headers':data['headers'],'rows':data['rows'],'caption':'','summary':'',
                            'metadata':{'row_count':len(data['rows']),'column_count':len(data['headers']) or max(map(len,data['rows']),default=0)},
                            'source_cells':references,'extraction_method':'local_model_verified','model_output_accepted':True}
                result['attempts'].append({'table_index':table_index,'chunk_index':chunk_index,'attempt':attempt+1,
                    'status':'complete' if accepted else 'partial','error':None if accepted else error,
                    'usage':extracted['usage'],'coverage':extracted['coverage'],
                    'model_chunks':extracted.get('chunks',[])})
                if accepted:break
                if error=='model_processing_incomplete':break
                options['instruction']=INSTRUCTION+' Previous output was rejected: '+error+'. Correct that error. '+(
                    'A value was in the wrong row/column. Expand rowspan values into EVERY occupied row; never substitute an empty string for a repeated spanned cell.'
                    if error=='source_cell_position_mismatch' else '')
            if accepted is None and source_fallback and complex_table and error not in {'table_time_budget','table_model_budget'} and time.monotonic()-started<seconds:
                model_error=error
                try:
                    strict_source_grid(table)  # Validate original row groups before chunk wrappers.
                    data=strict_source_grid(html.fromstring(fragment))
                    references,source_error=verify_table(data,fragment,row_count)
                    if source_error:raise ValueError(source_error)
                    references=original_cell_refs(table,fragment,references,body_start,row_count)
                    accepted={'headers':data['headers'],'rows':data['rows'],'caption':'','summary':'',
                        'metadata':{'row_count':len(data['rows']),'column_count':len(data['headers'])},
                        'source_cells':references,'extraction_method':'source_grid','model_output_accepted':False,
                        'model_rejection':model_error}
                except ValueError as source_error:
                    result.setdefault('source_grid_failures',[]).append({'table_index':table_index,'chunk_index':chunk_index,'reason':str(source_error)})
            result['coverage']['processed_chunks']+=1
            if accepted:
                valid.append({'chunk_index':chunk_index,'table':accepted,'fragment':fragment})
            else:partial(error);table_complete=False
        if valid:
            if any(item['table']['headers']!=valid[0]['table']['headers'] for item in valid):
                partial('inconsistent_chunk_headers');table_complete=False
            merged=algorithm._merge_chunk_results(valid)[0]
            # References are per source fragment; do not inherit the first chunk's cells as global provenance.
            merged.pop('source_cells',None)
            methods={item['table']['extraction_method'] for item in valid}
            merged['extraction_method']=next(iter(methods)) if len(methods)==1 else 'mixed_source_grid_and_local_model'
            merged['model_output_accepted']=all(item['table']['model_output_accepted'] for item in valid)
            structure=source_structure(table)
            merged.update(table_index=table_index,source_xpath=table.getroottree().getpath(table),
                source_html_sha256=digest(serialize(table)),caption=' '.join(table.xpath('./caption//text()')),
                summary=table.get('summary',''),complete=table_complete,
                header_rows=structure['header_rows'],header_paths=structure['header_paths'],
                header_path_refs=structure['header_path_refs'],
                structure_method='source_DOM_span_grid; output_values_verified_per_coordinate',
                nested_tables=[],
                chunks=[{'index':item['chunk_index'],'html':item['fragment'],'sha256':digest(item['fragment']),
                         'cells':item['table']['source_cells'],'extraction_method':item['table']['extraction_method'],
                         'model_output_accepted':item['table']['model_output_accepted'],
                         'model_rejection':item['table'].get('model_rejection')} for item in valid])
            result['tables'].append(merged)
        if table_complete:result['coverage']['processed_tables']+=1
    resolved={table['table_index']:table for table in result['tables']}
    for output in reversed(result['tables']):
        parent=tables[output['table_index']];structure=source_structure(parent)
        for child_index,child in enumerate(tables):
            if next(child.iterancestors('table'),None) is not parent:continue
            cell=next((node for node in child.iterancestors() if node.tag in {'td','th'}),None)
            cell_path=cell.getroottree().getpath(cell) if cell is not None else None
            positions=[{'section':section,'row':r,'column':c} for section,grid in
                [('header_rows',structure['header_positions']),('rows',structure['body_positions'])]
                for r,row in enumerate(grid) for c,path in enumerate(row) if path==cell_path]
            child_output=resolved.get(child_index)
            status='complete' if child_output and child_output['complete'] else 'partial' if child_output else 'not_processed'
            output['nested_tables'].append({'table_index':child_index,'source_xpath':child.getroottree().getpath(child),
                'source_html_sha256':digest(serialize(child)),'parent_cell_xpath':cell_path,'parent_cell_positions':positions,
                'flat_cell_text_includes_nested':True,'status':status,
                'resolved_table_index':child_index if child_output else None})
            if status!='complete':
                output['complete']=False;partial('nested_table_incomplete')
    result['coverage']['processed_tables']=sum(table['complete'] for table in result['tables'])
    result['source_grid_used']=any('source_grid' in table['extraction_method'] for table in result['tables'])
    result['model_output_complete']=bool(result['attempts']) and all(attempt['status']=='complete' for attempt in result['attempts']) and result['coverage']['complete']
    result['budget']={'calls_reserved':calls,'output_tokens_reserved':reserved}
    return result
