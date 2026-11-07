"""Bounded viewport stitching, adapted from Crawl4AI take_screenshot_scroller.

Frozen source 838869bdda93f25e6d50b3f50ae9577d9b19a586, Apache-2.0;
LICENSE.crawl4ai and NOTICE apply. Shared caller owns page and browser lifetime.
"""
from __future__ import annotations
import asyncio
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import time
import uuid
from PIL import Image
from ..core.jobs import JobControl,BudgetExceeded


def _options(options):
    values=dict(options or {})
    defaults={'max_pixels':16_000_000,'max_segments':32,'max_height':100000,
              'max_seconds':30,'scroll_delay_ms':100,'fixed_policy':'repeat'}
    if set(values)-set(defaults):raise ValueError('Unsupported stitched screenshot option')
    defaults.update(values)
    for key,low,high in [('max_pixels',100,64_000_000),('max_segments',1,128),('max_height',1,100000)]:
        if type(defaults[key]) is not int or not low<=defaults[key]<=high:raise ValueError('Invalid screenshot '+key)
    for key,low,high in [('max_seconds',.05,120),('scroll_delay_ms',0,2000)]:
        value=defaults[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError('Invalid screenshot '+key)
    if defaults['fixed_policy'] not in {'repeat','hide_after_first'}:raise ValueError('Invalid fixed_policy')
    return defaults


async def _settle(awaitable,on_cancel=None):
    task=asyncio.ensure_future(awaitable)
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:
            if on_cancel:on_cancel()
    return task.result()


async def capture_stitched(page,output,options=None,*,context=None,adapter=None,on_receipt=None):
    """Write actual captured pixels and an explicit receipt; never create an error image.

    On cancellation/control, cleanup and partial receipt settle before propagation.
    Options count device pixels; source coordinates and scroll offsets use CSS pixels.
    """
    cfg=_options(options);output=Path(output).resolve()
    if context is not None and not output.is_relative_to(context.paths.run(context.job_id).resolve()):
        raise ValueError('Screenshot output must stay inside the owned job directory')
    output.parent.mkdir(parents=True,exist_ok=True)
    receipt_path=output.with_suffix(output.suffix+'.receipt.json')
    started=time.monotonic();token='__research_stitch_'+uuid.uuid4().hex
    original_viewport=page.viewport_size;canvas=None;covered=0;scale=None;interrupted=None
    receipt={'status':'partial','path':None,'receipt_path':str(receipt_path),'segments':[],
             'fixed_policy':cfg['fixed_policy'],'coverage':{'complete':False,'reasons':[]},
             'source_coordinates':'CSS pixels','output_coordinates':'device pixels'}
    def limit(reason):
        if reason not in receipt['coverage']['reasons']:receipt['coverage']['reasons'].append(reason)
    def cancelled():
        nonlocal interrupted
        if interrupted is None:interrupted=asyncio.CancelledError()
        limit('capture_interrupted');receipt['status']='cancelled';receipt['coverage']['complete']=False
    async def evaluate(code,arg=None):
        return await (adapter.evaluate(page,code,arg) if adapter else page.evaluate(code,arg))
    async def control():
        if context is not None:await context.check_control()
    async def cleanup():
        try:
            async with asyncio.timeout(5):
                await evaluate('''token => {const s=globalThis[token];if(!s)return;
                  for(const item of s.fixed){if(item.node.isConnected && item.changed){
                    if(item.value)item.node.style.setProperty('visibility',item.value,item.priority);
                    else item.node.style.removeProperty('visibility');
                    item.node.getAttribute('style');
                    if(!item.hadStyle && item.node.style.length===0)item.node.removeAttribute('style');}}
                  window.scrollTo(s.x,s.y);s.style.remove();delete globalThis[token];}''',token)
                if original_viewport is not None and page.viewport_size!=original_viewport:
                    await page.set_viewport_size(original_viewport)
        except Exception as error:
            limit('screenshot_restore_failed');receipt['restore_error']=type(error).__name__
    try:
        async with asyncio.timeout(cfg['max_seconds']):
            await control()
            initial=await evaluate('''token => {const nodes=Array.from(document.querySelectorAll('*'));
              const fixed=[];for(const node of nodes.slice(0,20000)){
                if(['fixed','sticky'].includes(getComputedStyle(node).position))fixed.push({node,
                  value:node.style.getPropertyValue('visibility'),priority:node.style.getPropertyPriority('visibility'),hadStyle:node.hasAttribute('style'),changed:false});}
              const style=document.createElement('style');style.textContent='html,body{scroll-behavior:auto!important;scroll-snap-type:none!important}';
              globalThis[token]={x:scrollX,y:scrollY,fixed,style};document.documentElement.append(style);
              return {width:innerWidth,height:innerHeight,page_width:document.documentElement.scrollWidth,
                page_height:Math.max(document.body?.scrollHeight||0,document.documentElement.scrollHeight),
                dpr:devicePixelRatio,fixed_count:fixed.length,dom_scan_limited:nodes.length>20000};}''',token)
            receipt['initial']=initial
            if initial['dom_scan_limited'] and cfg['fixed_policy']!='repeat':limit('fixed_element_scan_limit')
            if initial['page_width']>initial['width']:limit('horizontal_overflow_not_captured')
            width,height=initial['width'],initial['height'];dpr=initial['dpr']
            if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in (width,height,dpr)):
                raise ValueError('Invalid browser dimensions')
            target=min(initial['page_height'],cfg['max_height'],height*cfg['max_segments'],
                       math.floor(cfg['max_pixels']/(math.ceil(width*dpr)*dpr)))
            if math.ceil(width*dpr)*math.ceil(height*dpr)>cfg['max_pixels']:
                limit('viewport_pixel_budget');target=0
            if target<1:limit('screenshot_pixel_budget')
            if target<initial['page_height']:limit('screenshot_size_or_segment_budget')
            receipt['coverage']['target_css_height']=target
            while covered<target and len(receipt['segments'])<cfg['max_segments']:
                await control()
                offset=await evaluate('''arg => {const s=globalThis[arg.token];
                  if(arg.hide && s)for(const item of s.fixed){item.node.style.setProperty('visibility','hidden','important');item.changed=true;}
                  window.scrollTo(0,arg.y);return {x:scrollX,y:scrollY};}''',
                  {'token':token,'y':covered,'hide':bool(covered and cfg['fixed_policy']=='hide_after_first')})
                if cfg['scroll_delay_ms']:await asyncio.sleep(cfg['scroll_delay_ms']/1000)
                actual=await evaluate('''() => ({x:scrollX,y:scrollY,width:innerWidth,height:innerHeight,
                  page_height:Math.max(document.body?.scrollHeight||0,document.documentElement.scrollHeight)})''')
                if actual['width']!=width or actual['height']!=height:
                    limit('viewport_changed_during_capture');break
                if actual['page_height']!=initial['page_height']:limit('page_height_changed_during_capture')
                if actual['x']!=0 or actual['y']>covered or covered>=actual['y']+height:
                    limit('scroll_position_unavailable');break
                remaining=max(1,int((cfg['max_seconds']-(time.monotonic()-started))*1000))
                raw=await page.screenshot(full_page=False,type='png',timeout=remaining)
                with Image.open(BytesIO(raw)) as image:
                    if image.width*image.height>cfg['max_pixels']:
                        limit('viewport_pixel_budget');break
                    image.load();factor=image.width/width
                    if not math.isclose(image.height/height,factor,rel_tol=.002,abs_tol=.002):
                        limit('screenshot_pixel_scale_mismatch');break
                    if scale is None:
                        scale=factor
                        target=min(target,math.floor(cfg['max_pixels']/(image.width*scale)))
                        if target<initial['page_height']:limit('screenshot_size_or_segment_budget')
                        if target<1:limit('screenshot_pixel_budget');break
                        pixel_height=min(math.ceil(target*scale),cfg['max_pixels']//image.width)
                        target=min(target,pixel_height/scale)
                        canvas=Image.new('RGB',(image.width,pixel_height))
                        receipt['device_scale']=scale
                    elif not math.isclose(scale,factor,rel_tol=.0001):
                        limit('screenshot_pixel_scale_changed');break
                    end=min(target,actual['y']+height)
                    top=round((covered-actual['y'])*scale);bottom=min(image.height,round((end-actual['y'])*scale))
                    strip=image.crop((0,top,image.width,bottom)).convert('RGB')
                    try:canvas.paste(strip,(0,round(covered*scale)))
                    finally:strip.close()
                    receipt['segments'].append({'source_css_range':[covered,end],'scroll_y':actual['y'],
                        'viewport_pixels':[image.width,image.height],'crop_pixel_range':[top,bottom],
                        'screenshot_sha256':hashlib.sha256(raw).hexdigest()})
                    covered=end
            if covered<target:limit('capture_incomplete')
    except (asyncio.CancelledError,JobControl,BudgetExceeded) as error:
        interrupted=error;limit('capture_interrupted');receipt['status']='cancelled' if isinstance(error,asyncio.CancelledError) else 'partial'
    except TimeoutError:
        limit('screenshot_time_budget')
    except Exception as error:
        limit('screenshot_capture_failed');receipt['error_type']=type(error).__name__
    finally:
        await _settle(cleanup(),cancelled)
        def persist():
            nonlocal canvas
            try:
                if canvas is not None and covered>0:
                    image=canvas.crop((0,0,canvas.width,min(canvas.height,round(covered*scale))))
                    temp=output.with_suffix(output.suffix+'.'+uuid.uuid4().hex+'.tmp')
                    try:
                        image.save(temp,format='PNG');temp.replace(output)
                        receipt.update(path=str(output),sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                                       pixels=[image.width,image.height],bytes=output.stat().st_size)
                    finally:
                        image.close();temp.unlink(missing_ok=True)
            except OSError as error:
                limit('screenshot_export_failed');receipt['error_type']=type(error).__name__;receipt['path']=None
            finally:
                if canvas is not None:canvas.close()
            receipt['coverage']['captured_css_height']=covered
            receipt['coverage']['complete']=bool(receipt['path']) and not receipt['coverage']['reasons']
            if receipt['coverage']['complete']:receipt['status']='complete'
            receipt['elapsed_seconds']=round(time.monotonic()-started,3)
            try:receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError as error:
                limit('screenshot_receipt_export_failed');receipt['receipt_path']=None;receipt['error_type']=type(error).__name__
                receipt['coverage']['complete']=False
                if receipt['status']=='complete':receipt['status']='partial'
        await _settle(asyncio.to_thread(persist),cancelled)
        if isinstance(interrupted,asyncio.CancelledError) and receipt['receipt_path']:
            try:receipt_path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError:receipt['receipt_path']=None
    if on_receipt is not None:
        try:on_receipt(receipt)
        except Exception as error:
            limit('screenshot_receipt_delivery_failed');receipt['coverage']['complete']=False
            if receipt['status']=='complete':receipt['status']='partial'
            receipt['delivery_error']=type(error).__name__
    if interrupted is not None:raise interrupted
    return receipt
