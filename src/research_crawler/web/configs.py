"""Sparse typed configuration over the shared runtime, derived from Crawl4AI.

Original async_configs.py clone/dump/load and URL matching contracts, frozen
838869bdda93f25e6d50b3f50ae9577d9b19a586; Apache-2.0 and attribution in LICENSE.crawl4ai/NOTICE.
Only explicit options override product defaults. Unsupported source fields remain
round-trippable but execution rejects them instead of silently dropping behavior.
"""
from __future__ import annotations
import copy
from enum import Enum
from fnmatch import fnmatch
import json
import math
from pathlib import Path

from .cache_context import CacheMode

INVENTORY=json.loads(Path(__file__).with_name('config-inventory.json').read_text(encoding='utf-8'))

class MatchMode(Enum):
    OR='or'
    AND='and'

class Provenance(Enum):
    TRUSTED='trusted'
    UNTRUSTED='untrusted'

class UntrustedConfigError(ValueError):pass
class UnsupportedConfigError(ValueError):pass

def _data(value,*,trusted=False,depth=0):
    if depth>30:raise ValueError('Configuration nesting exceeds30')
    if isinstance(value,Config):return value
    if isinstance(value,Enum):return value.value
    if callable(value):
        if trusted:return value
        raise UntrustedConfigError('Callable configuration requires explicit trusted Python construction')
    if value is None or type(value) in {str,bool,int}:return value
    if type(value) is float:
        if not math.isfinite(value):raise ValueError('Non-finite config number')
        return value
    if isinstance(value,(list,tuple)):return [_data(v,trusted=trusted,depth=depth+1) for v in value]
    if isinstance(value,dict):
        if any(not isinstance(k,str) for k in value):raise ValueError('Configuration keys must be strings')
        return {k:_data(v,trusted=trusted,depth=depth+1) for k,v in value.items()}
    raise UntrustedConfigError('Unsupported configuration object')

def to_serializable_dict(value):
    if isinstance(value,Config):return {'type':type(value).__name__,'params':{k:to_serializable_dict(v) for k,v in value._values.items()}}
    if isinstance(value,Enum):return {'type':type(value).__name__,'params':value.value}
    value=_data(value)
    if isinstance(value,dict):return {'type':'dict','value':{k:to_serializable_dict(v) for k,v in value.items()}}
    if isinstance(value,list):return [to_serializable_dict(v) for v in value]
    return value

def from_serializable_dict(value,provenance=Provenance.UNTRUSTED):
    # A closed registry; no import, eval, module name or arbitrary constructor.
    value=_data(value,trusted=provenance==Provenance.TRUSTED)
    typed=lambda item:isinstance(item,dict) and 'type' in item and ('params' in item or item['type']=='dict' and 'value' in item)
    if typed(value):
        if value['type']=='dict':
            if set(value)!={'type','value'} or not isinstance(value['value'],dict):raise UntrustedConfigError('Malformed dictionary envelope')
            unwrapped={k:from_serializable_dict(v,provenance) for k,v in value['value'].items()}
            if provenance==Provenance.UNTRUSTED and typed(unwrapped):raise UntrustedConfigError('Wrapped constructor cannot bypass the registry')
            return unwrapped
        if set(value)!={'type','params'}:raise UntrustedConfigError('Serialized config must contain only type and params')
        name=value['type'];params=value['params']
        if name in {'MatchMode','CacheMode'}:return {'MatchMode':MatchMode,'CacheMode':CacheMode}[name](params)
        if name not in REGISTRY or not isinstance(params,dict):raise UntrustedConfigError('Unknown serialized configuration type')
        return REGISTRY[name](provenance=provenance,**{k:from_serializable_dict(v,provenance) for k,v in params.items()})
    if isinstance(value,dict):return {k:from_serializable_dict(v,provenance) for k,v in value.items()}
    if isinstance(value,list):return [from_serializable_dict(v,provenance) for v in value]
    return value

class Config:
    def __init__(self,*,provenance=Provenance.UNTRUSTED,**kwargs):
        if not isinstance(provenance,Provenance):raise ValueError('Invalid provenance')
        unknown=set(kwargs)-set(INVENTORY[type(self).__name__]['fields'])
        if unknown:raise ValueError('Unknown '+type(self).__name__+' fields: '+', '.join(sorted(unknown)))
        self._provenance=provenance
        self._values={k:_data(v,trusted=provenance==Provenance.TRUSTED) for k,v in kwargs.items()}
        self._validate()
    def _validate(self):
        for key,value in self._values.items():
            if value is None:continue
            default=INVENTORY[type(self).__name__]['defaults'].get(key,'')
            if default in {'True','False'} and type(value) is not bool:raise ValueError(key+' requires a boolean')
            if key in {'browser_type','cdp_url','browser_context_id','target_id','channel','chrome_channel','css_selector','excluded_selector','wait_for','wait_until','session_id','locale','timezone_id','user_agent','method','c4a_script','container_selector','query'} and not isinstance(value,str):raise ValueError(key+' requires text')
            if key in {'headers','viewport','user_agent_generator_config'} and not isinstance(value,dict):raise ValueError(key+' requires an object')
            if key in {'excluded_tags','exclude_domains','exclude_social_media_domains','init_scripts','extra_args'} and (not isinstance(value,list) or any(not isinstance(v,str) for v in value)):raise ValueError(key+' requires a list of strings')
        matcher=self._values.get('url_matcher')
        if matcher is not None:
            entries=matcher if isinstance(matcher,list) else [matcher]
            if any(not isinstance(v,str) and not callable(v) for v in entries):raise ValueError('URL matchers must be patterns or trusted callables')
        if 'match_mode' in self._values:self._values['match_mode']=MatchMode(self._values['match_mode']).value
    def __getattr__(self,key):
        values=object.__getattribute__(self,'__dict__').get('_values',{})
        if key in values:return copy.deepcopy(values[key])
        raise AttributeError(key+' was not explicitly configured')
    def to_dict(self):return copy.deepcopy(self._values)
    def dump(self):return to_serializable_dict(self)
    def clone(self,**kwargs):return type(self)(provenance=self._provenance,**{**self.to_dict(),**kwargs})
    @classmethod
    def from_kwargs(cls,kwargs,provenance=Provenance.UNTRUSTED):return cls(provenance=provenance,**kwargs)
    @classmethod
    def load(cls,data,provenance=Provenance.UNTRUSTED):
        result=from_serializable_dict(data,provenance)
        if isinstance(result,dict):result=cls(provenance=provenance,**result)
        if not isinstance(result,cls):raise ValueError('Serialized configuration type mismatch')
        return result

class BrowserConfig(Config):pass
class HTTPCrawlerConfig(Config):pass
class GeolocationConfig(Config):pass
class LinkPreviewConfig(Config):pass
class VirtualScrollConfig(Config):pass
class CrawlerRunConfig(Config):
    def is_match(self,url):
        matcher=self._values.get('url_matcher')
        if matcher is None:return True
        if callable(matcher):return bool(matcher(url))
        if isinstance(matcher,str):return fnmatch(url,matcher)
        results=[bool(v(url)) if callable(v) else fnmatch(url,v) for v in matcher]
        if not results:return False
        return all(results) if self._values.get('match_mode','or')=='and' else any(results)

REGISTRY={cls.__name__:cls for cls in (BrowserConfig,CrawlerRunConfig,HTTPCrawlerConfig,GeolocationConfig,LinkPreviewConfig,VirtualScrollConfig)}
CONTEXT={'device_scale_factor','ignore_https_errors','java_script_enabled','locale','timezone_id','geolocation','user_agent'}
BROWSER_DIRECT={'browser_type','headless','cdp_url','browser_context_id','target_id','channel','viewport','accept_downloads','headers','init_scripts','text_mode','light_mode','max_pages_before_recycle','avoid_css','avoid_ads','memory_saving_mode'}
RUN_DIRECT={'word_count_threshold','only_text','css_selector','excluded_tags','keep_data_attributes','remove_forms',
 'fetch_ssl_certificate','session_id','wait_until','wait_for','wait_for_images','adjust_viewport_to_content','js_code','js_code_before_wait','c4a_script',
 'js_only','scan_full_page','max_scroll_steps','process_iframes','flatten_shadow_dom','remove_overlay_elements','screenshot','pdf',
 'image_description_min_word_threshold','image_score_threshold','exclude_external_images','exclude_all_images',
 'exclude_social_media_domains','exclude_external_links','exclude_social_media_links','exclude_domains','score_links',
 'capture_network_requests','capture_console_messages','stream','prefetch',
 'override_navigator','simulate_user','magic','wait_for_timeout','ignore_body_visibility',
 'body_visibility_timeout','screenshot_wait_for'}
RUN_ALIAS={'excluded_selector':'excluded_selectors','capture_mhtml':'mhtml','check_cache_freshness':'cache_validate','method':'http_method'}

def _merge(destination,key,value):
    if key in destination and destination[key]!=value:raise ValueError('Conflicting configuration for '+key)
    destination[key]=copy.deepcopy(value)

def _nested(value,cls):
    if isinstance(value,cls):return value.to_dict()
    if isinstance(value,dict):return cls.load(value).to_dict()
    raise ValueError('Expected '+cls.__name__+' configuration')

def _number(value,key,low,high):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError(key+' is outside supported bounds')
    return value

def _params(config):
    values=config.to_dict();result={};context={};kind=type(config)
    handled=set()
    for key,value in values.items():
        if value is None:handled.add(key);continue
        if key in CONTEXT:
            if key=='geolocation':value=_nested(value,GeolocationConfig)
            context[key]=value;handled.add(key)
        elif kind is BrowserConfig and key in BROWSER_DIRECT:_merge(result,key,value);handled.add(key)
        elif kind is CrawlerRunConfig and key in RUN_DIRECT:_merge(result,key,value);handled.add(key)
        elif kind is CrawlerRunConfig and key in RUN_ALIAS:_merge(result,RUN_ALIAS[key],value);handled.add(key)
        elif kind in {BrowserConfig,CrawlerRunConfig} and key in {'user_agent_mode','user_agent_generator_config'}:
            _merge(result,key,value);handled.add(key)
        elif kind is CrawlerRunConfig and key=='proxy_rotation_strategy':
            if not isinstance(value,dict):raise ValueError('proxy_rotation_strategy requires a named proxy policy object')
            _merge(result,'proxy_policy',value);handled.add(key)
    if kind is BrowserConfig:
        for name,target in [('extra_args','launch_args'),('chrome_channel','channel')]:
            if name in values and values[name] is not None:_merge(result,target,values[name]);handled.add(name)
        if any(key in values for key in ('viewport_width','viewport_height')):
            viewport={'width':values.get('viewport_width',1080),'height':values.get('viewport_height',600)}
            for key,value in viewport.items():
                if type(value) is not int:raise ValueError('Viewport dimensions must be integers')
                _number(value,'viewport_'+key,1,16384)
            _merge(result,'viewport',viewport);handled.update({'viewport_width','viewport_height'})
    if kind is CrawlerRunConfig:
        for key,target,factor in [('page_timeout','timeout_seconds',.001),('delay_before_return_html','settle_ms',1000),('scroll_delay','scroll_delay_ms',1000)]:
            if key in values and values[key] is not None:
                value=_number(values[key],key,1000 if key=='page_timeout' else 0,120000 if key=='page_timeout' else 2 if key=='scroll_delay' else 5)
                _merge(result,target,value*factor);handled.add(key)
        if values.get('cache_mode') is not None:_merge(result,'cache_mode',CacheMode(values['cache_mode']).value);handled.add('cache_mode')
        if 'force_viewport_screenshot' in values:_merge(result,'screenshot_full_page',not values['force_viewport_screenshot']);handled.add('force_viewport_screenshot')
        if 'process_in_browser' in values:_merge(result,'render','browser' if values['process_in_browser'] else 'auto');handled.add('process_in_browser')
        if values.get('link_preview_config') is not None:_merge(result,'link_preview_config',_nested(values['link_preview_config'],LinkPreviewConfig));handled.add('link_preview_config')
        if values.get('virtual_scroll_config') is not None:_merge(result,'virtual_scroll_config',_nested(values['virtual_scroll_config'],VirtualScrollConfig));handled.add('virtual_scroll_config')
        handled.update({'url_matcher','match_mode'})
    if kind is HTTPCrawlerConfig:
        for key,target in [('method','http_method'),('headers','headers'),('json','http_json'),('follow_redirects','follow_redirects')]:
            if key in values and values[key] is not None:_merge(result,target,values[key]);handled.add(key)
        if 'data' in values and values['data'] is not None:
            if not isinstance(values['data'],(str,dict)):raise ValueError('HTTP data requires a string body or form mapping')
            _merge(result,'http_form' if isinstance(values['data'],dict) else 'http_body',values['data']);handled.add('data')
        if values.get('verify_ssl') is not None:
            _merge(result,'verify_ssl',values['verify_ssl']);handled.add('verify_ssl')
        _merge(result,'render','http')
    unsupported=set(values)-handled
    if unsupported:raise UnsupportedConfigError('Not integrated for '+kind.__name__+': '+', '.join(sorted(unsupported)))
    if context:result['browser_options']=context
    return result

def normalize_params(params):
    if not isinstance(params,dict):raise ValueError('Web parameters must be an object')
    result=copy.deepcopy(params)
    for key,cls in [('browser_config',BrowserConfig),('run_config',CrawlerRunConfig),('http_config',HTTPCrawlerConfig)]:
        if key not in result:continue
        raw=result.pop(key);config=raw if isinstance(raw,cls) else cls.load(raw)
        if isinstance(config,CrawlerRunConfig):
            urls=result.get('urls',[]);urls=[urls] if isinstance(urls,str) else urls
            if not all(config.is_match(url) for url in urls):raise ValueError('run_config does not match every supplied URL; select configurations per URL explicitly')
        for option,value in _params(config).items():
            if option=='browser_options':
                context=dict(result.get(option,{}))
                for k,v in value.items():_merge(context,k,v)
                result[option]=context
            else:_merge(result,option,value)
    from ..core.browser_behavior import behavior_options
    behavior_options(result)
    return result

def select_url_config(configs,url):
    if not isinstance(configs,list) or not configs or len(configs)>100:raise ValueError('Expected1..100 run configurations')
    for raw in configs:
        config=raw if isinstance(raw,CrawlerRunConfig) else CrawlerRunConfig.load(raw)
        if config.is_match(url):return config
    raise ValueError('No run configuration matched URL')
