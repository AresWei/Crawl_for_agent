"""Per-URL source run configurations, sharing the parent job and owned resources."""
import copy
import hashlib
import json
from .configs import CrawlerRunConfig,_params


class ParamsContext:
    def __init__(self,parent,params):
        object.__setattr__(self,'_parent',parent)
        object.__setattr__(self,'params',params)
        object.__setattr__(self,'resources',parent.resources)
    def __getattr__(self,name):return getattr(self._parent,name)
    def __setattr__(self,name,value):
        if name in {'_parent','params','resources'}:object.__setattr__(self,name,value)
        else:setattr(self._parent,name,value)
    def __copy__(self):
        result=type(self)(self._parent,self.params);result.resources=self.resources;return result


class URLRunConfigs:
    def __init__(self,params):
        values=params.get('run_configs')
        if not isinstance(values,list) or not 1<=len(values)<=32:
            raise ValueError('run_configs requires 1..32 ordered run configurations')
        self.base={key:value for key,value in params.items() if key!='run_configs'}
        self.entries=[]
        for value in values:
            config=value if isinstance(value,CrawlerRunConfig) else CrawlerRunConfig.load(value)
            overrides=_params(config)
            if set(overrides)&{'stream','session_id','js_only'}:
                raise ValueError('stream and live-session control belong to the enclosing job, not per-URL configs')
            self.entries.append((config,overrides))

    def resolve(self,url):
        for index,(config,overrides) in enumerate(self.entries):
            if not config.is_match(url):continue
            params=copy.deepcopy(self.base)
            for key,value in overrides.items():
                if key=='browser_options':params[key]={**params.get(key,{}),**copy.deepcopy(value)}
                else:params[key]=copy.deepcopy(value)
            signature=hashlib.sha256(json.dumps(overrides,sort_keys=True,default=str).encode()).hexdigest()
            return params,{'index':index,'options_sha256':signature,'scope':'first_matching_run_configuration'}
        raise ValueError('No per-URL run configuration matched')
