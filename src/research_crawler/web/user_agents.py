"""Attributed local User-Agent policies from pinned Crawl4AI.

Source 838869bdda93f25e6d50b3f50ae9577d9b19a586, user_agent_generator.py.
Apache-2.0; LICENSE.crawl4ai and NOTICE apply. Caller owns network and identity.
"""
from __future__ import annotations
import asyncio
from abc import ABC, abstractmethod
import copy
import hashlib
import json
import math
import random
import re
from typing import Optional, Literal, List, Dict, Tuple, Union
from lxml import html
import httpx

DEFAULT_UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36'
ONLINE_SOURCE='https://www.useragents.me/'


def _ua(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(ord(c)<32 or ord(c)>126 for c in value):
        raise ValueError('User-Agent must be 1..2048 printable ASCII characters')
    return value


def _strings(value,name):
    if isinstance(value,str):value=[value]
    if not isinstance(value,list) or not value or any(not isinstance(v,str) or not v.strip() or len(v)>100 for v in value):
        raise ValueError(name+' must contain nonempty names')
    return value


def _configuration(params):
    explicit=[]
    for value in [params.get('user_agent'),params.get('browser_options',{}).get('user_agent')]:
        if value is not None:explicit.append(_ua(value))
    for key,value in params.get('headers',{}).items():
        if key.lower()=='user-agent':explicit.append(_ua(value))
    if len(set(explicit))>1:raise ValueError('Conflicting HTTP/browser User-Agent configuration')
    mode=params.get('user_agent_mode') or 'explicit'
    if mode not in {'explicit','random','synthetic','online'}:raise ValueError('Unsupported user_agent_mode')
    options=copy.deepcopy(params.get('user_agent_generator_config') or {})
    if not isinstance(options,dict):raise ValueError('user_agent_generator_config must be an object')
    if mode=='explicit' and options:raise ValueError('Generator configuration requires a generation mode')
    allowed={'device_type','os_type','device_brand','browser_type','num_browsers'} if mode=='synthetic' else {'browsers','os','min_version','platforms','pct_threshold','fallback'}
    if set(options)-allowed:raise ValueError('Unknown User-Agent generator option')
    if mode!='explicit' and explicit:raise ValueError('Choose an explicit User-Agent or a generator')
    if mode=='synthetic':
        for key,choices in [('device_type',{'desktop','mobile'}),('os_type',{'windows','macos','linux','android','ios'}),('browser_type',{'chrome','firefox','safari','edge'})]:
            if options.get(key) is not None and options[key] not in choices:raise ValueError('Unsupported synthetic '+key)
        count=options.get('num_browsers',3)
        if type(count) is not int or count not in {1,2,3}:raise ValueError('num_browsers must be1..3')
        if options.get('device_type')=='desktop' and options.get('os_type') in {'android','ios'} or options.get('device_type')=='mobile' and options.get('os_type') in {'windows','macos','linux'}:
            raise ValueError('Conflicting synthetic device/OS constraints')
    else:
        for key in ('browsers','os','platforms'):
            if options.get(key) is not None:options[key]=_strings(options[key],key)
        for key in ('min_version','pct_threshold'):
            value=options.get(key)
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0):raise ValueError('Invalid '+key)
        if options.get('fallback') is not None:_ua(options['fallback'])
        if mode=='random' and options.get('pct_threshold') is not None:raise ValueError('ValidUA source has no population-share filter; use online mode')
    return {'mode':mode,'explicit':explicit[0] if explicit else None,'options':options}


async def _online_agents(client):
    if client is None:raise ValueError('Online User-Agent requires the shared HTTP client')
    async with asyncio.timeout(5):
        response=await client.send(client.build_request('GET',ONLINE_SOURCE,headers={'Accept':'text/html,application/xhtml+xml'}),stream=True,follow_redirects=False)
        try:
            response.raise_for_status();data=bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data)>1_000_000:raise ValueError('User-Agent source exceeds byte limit')
            tree=html.fromstring(bytes(data))
            matches=tree.cssselect('#most-common-desktop-useragents-json-csv > div:nth-child(1) > textarea')
            if not matches:raise ValueError('User-Agent source layout changed')
            rows=json.loads(matches[0].text or '')
            if not isinstance(rows,list) or len(rows)>5000:raise ValueError('Invalid User-Agent source rows')
            result=[]
            for row in rows:
                if not isinstance(row,dict):continue
                try:ua=_ua(row.get('ua'));pct=float(row.get('pct',0))
                except (ValueError,TypeError):continue
                if not math.isfinite(pct) or pct<0:continue
                result.append({'ua':ua,'pct':pct})
            return result,{'url':ONLINE_SOURCE,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'rows':len(result)}
        finally:await response.aclose()


async def resolve_user_agent(params,snapshot=None,*,client=None):
    """Choose once; persist this receipt and reuse it after the caller resumes.

    No choice returns None, preserving existing native browser/product defaults.
    Client hints are source-derived strings, not a claim about browser UAData.
    """
    config=_configuration(params)
    if config['mode']=='explicit' and config['explicit'] is None:return None
    signature=hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if snapshot is not None:
        if not isinstance(snapshot,dict) or snapshot.get('config_sha256')!=signature:
            raise ValueError('Saved User-Agent belongs to a different configuration')
        _ua(snapshot.get('user_agent'))
        if snapshot.get('choice_sha256')!=hashlib.sha256(snapshot['user_agent'].encode()).hexdigest():raise ValueError('Saved User-Agent integrity mismatch')
        restored=copy.deepcopy(snapshot)
        restored['headers']={'User-Agent':restored['user_agent']}
        hints=UAGen.generate_client_hints(restored['user_agent'])
        restored['client_hints']=hints
        if hints:restored['headers']['Sec-CH-UA']=hints
        return restored
    mode=config['mode'];options=config['options'];limits=[];source_details=None
    if mode=='explicit':selected=config['explicit']
    elif mode=='synthetic':
        generator=UserAgentGenerator()
        brand=options.get('device_brand');os_name=options.get('os_type')
        if brand is not None:
            groups={**generator.desktop_platforms,**generator.mobile_platforms}
            if not os_name or brand not in groups[os_name]:raise ValueError('device_brand requires a matching os_type and known source device')
        browser=options.get('browser_type');count=options.get('num_browsers',3)
        if browser:
            combinations=[entry for entry in generator.browser_combinations[count] if browser in entry]
            if not combinations:raise ValueError('Requested browser has no source stack for num_browsers')
            generator.browser_combinations[count]=combinations
        selected=generator.generate(**options)
        limits.append('Synthetic source templates contain historical browser versions and may not describe a real browser.')
    elif mode=='random':
        try:from fake_useragent import UserAgent
        except ImportError:raise ImportError('Install the optional fake-useragent dependency to use random mode') from None
        selection={k:v for k,v in options.items() if k!='pct_threshold'}
        selected=UserAgent(browsers=selection.pop('browsers',None) or ['Chrome','Firefox','Edge'],
            os=selection.pop('os',None) or ['Windows','Mac OS X'],platforms=selection.pop('platforms',None) or ['desktop'],**selection).random
        limits.append('Bundled User-Agent dataset selection does not verify the executing browser identity.')
    else:
        if client is None:raise ValueError('Online User-Agent requires the shared HTTP client')
        try:agents,source_details=await _online_agents(client)
        except (httpx.HTTPError,TimeoutError,ValueError,html.etree.ParserError) as error:
            agents=[];source_details={'url':ONLINE_SOURCE,'status':'unavailable','error_type':type(error).__name__}
            limits.append('Online User-Agent source unavailable; fallback selection is not live source evidence.')
        for key in ('browsers','os','platforms'):
            if options.get(key):agents=[entry for entry in agents if any(v.lower() in entry['ua'].lower() for v in options[key])]
        if options.get('pct_threshold') is not None:agents=[entry for entry in agents if entry['pct']>=options['pct_threshold']]
        if options.get('min_version'):
            def compatible(entry):
                tokens={'chrome':'Chrome','chromium':'Chrome','firefox':'Firefox','edge':'Edg','safari':'Version'}
                requested=[tokens.get(name.lower()) for name in options.get('browsers',[])]
                requested=[name for name in requested if name]
                if not options.get('browsers'):requested=[name for name in ('Edg','Firefox','Chrome','Version') if name+'/' in entry['ua']][:1]
                versions=[v for name in requested for v in re.findall(re.escape(name)+r'/(\d+(?:\.\d+)?)',entry['ua'])]
                return bool(versions) and max(float(v) for v in versions)>=options['min_version']
            agents=[entry for entry in agents if compatible(entry)]
        selected=agents[0]['ua'] if agents else options.get('fallback',DEFAULT_UA)
        if not agents:limits.append('No source row matched; explicit/default historical fallback selected.')
        limits.append('Online source filtering retains upstream substring platform matching; source labels are not browser attestation.')
    selected=_ua(selected);hints=UAGen.generate_client_hints(selected)
    headers={'User-Agent':selected}
    if hints:headers['Sec-CH-UA']=hints
    return {'config_sha256':signature,'choice_sha256':hashlib.sha256(selected.encode()).hexdigest(),
            'user_agent':selected,'client_hints':hints,'headers':headers,'source':mode,
            'source_details':source_details,'limitations':limits+['Header hints do not rewrite navigator.userAgentData or prove fingerprint consistency.']}

class UAGen(ABC):

    @abstractmethod
    def generate(self, browsers: Optional[List[str]]=None, os: Optional[Union[str, List[str]]]=None, min_version: float=0.0, platforms: Optional[Union[str, List[str]]]=None, pct_threshold: Optional[float]=None, fallback: str='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/116.0.0.0 Safari/537.36') -> Union[str, Dict]:
        pass

    @staticmethod
    def generate_client_hints(user_agent: str) -> str:
        """Generate Sec-CH-UA header value based on user agent string"""

        def _parse_user_agent(user_agent: str) -> Dict[str, str]:
            """Parse a user agent string to extract browser and version information"""
            browsers = {'chrome': 'Chrome/(\\d+)', 'edge': 'Edg/(\\d+)', 'safari': 'Version/(\\d+)', 'firefox': 'Firefox/(\\d+)'}
            result = {}
            for browser, pattern in browsers.items():
                match = re.search(pattern, user_agent)
                if match:
                    result[browser] = match.group(1)
            return result
        browsers = _parse_user_agent(user_agent)
        hints = []
        if 'chrome' in browsers:
            hints.append(f'''"Chromium";v="{browsers['chrome']}"''')
            hints.append('"Not_A Brand";v="8"')
            if 'edge' in browsers:
                hints.append(f'''"Microsoft Edge";v="{browsers['edge']}"''')
            else:
                hints.append(f'''"Google Chrome";v="{browsers['chrome']}"''')
        elif 'firefox' in browsers:
            return '""'
        elif 'safari' in browsers:
            hints.append(f'''"Safari";v="{browsers['safari']}"''')
            hints.append('"Not_A Brand";v="8"')
        return ', '.join(hints)

class UserAgentGenerator:
    """
    Generate random user agents with specified constraints.

    Attributes:
        desktop_platforms (dict): A dictionary of possible desktop platforms and their corresponding user agent strings.
        mobile_platforms (dict): A dictionary of possible mobile platforms and their corresponding user agent strings.
        browser_combinations (dict): A dictionary of possible browser combinations and their corresponding user agent strings.
        rendering_engines (dict): A dictionary of possible rendering engines and their corresponding user agent strings.
        chrome_versions (list): A list of possible Chrome browser versions.
        firefox_versions (list): A list of possible Firefox browser versions.
        edge_versions (list): A list of possible Edge browser versions.
        safari_versions (list): A list of possible Safari browser versions.
        ios_versions (list): A list of possible iOS browser versions.
        android_versions (list): A list of possible Android browser versions.

        Methods:
            generate_user_agent(
                platform: Literal["desktop", "mobile"] = "desktop",
                browser: str = "chrome",
                rendering_engine: str = "chrome_webkit",
                chrome_version: Optional[str] = None,
                firefox_version: Optional[str] = None,
                edge_version: Optional[str] = None,
                safari_version: Optional[str] = None,
                ios_version: Optional[str] = None,
                android_version: Optional[str] = None
            ): Generates a random user agent string based on the specified parameters.
    """

    def __init__(self):
        self.desktop_platforms = {'windows': {'10_64': '(Windows NT 10.0; Win64; x64)', '10_32': '(Windows NT 10.0; WOW64)'}, 'macos': {'intel': '(Macintosh; Intel Mac OS X 10_15_7)', 'newer': '(Macintosh; Intel Mac OS X 10.15; rv:109.0)'}, 'linux': {'generic': '(X11; Linux x86_64)', 'ubuntu': '(X11; Ubuntu; Linux x86_64)', 'chrome_os': '(X11; CrOS x86_64 14541.0.0)'}}
        self.mobile_platforms = {'android': {'samsung': '(Linux; Android 13; SM-S901B)', 'pixel': '(Linux; Android 12; Pixel 6)', 'oneplus': '(Linux; Android 13; OnePlus 9 Pro)', 'xiaomi': '(Linux; Android 12; M2102J20SG)'}, 'ios': {'iphone': '(iPhone; CPU iPhone OS 16_5 like Mac OS X)', 'ipad': '(iPad; CPU OS 16_5 like Mac OS X)'}}
        self.browser_combinations = {1: [['chrome'], ['firefox'], ['safari'], ['edge']], 2: [['gecko', 'firefox'], ['chrome', 'safari'], ['webkit', 'safari']], 3: [['chrome', 'safari', 'edge'], ['webkit', 'chrome', 'safari']]}
        self.rendering_engines = {'chrome_webkit': 'AppleWebKit/537.36', 'safari_webkit': 'AppleWebKit/605.1.15', 'gecko': ['Gecko/20100101', 'Gecko/20100101', 'Gecko/2010010']}
        self.chrome_versions = ['Chrome/119.0.6045.199', 'Chrome/118.0.5993.117', 'Chrome/117.0.5938.149', 'Chrome/116.0.5845.187', 'Chrome/115.0.5790.171']
        self.edge_versions = ['Edg/119.0.2151.97', 'Edg/118.0.2088.76', 'Edg/117.0.2045.47', 'Edg/116.0.1938.81', 'Edg/115.0.1901.203']
        self.safari_versions = ['Safari/537.36', 'Safari/605.1.15', 'Safari/604.1', 'Safari/602.1', 'Safari/601.5.17']
        self.firefox_versions = ['Firefox/119.0', 'Firefox/118.0.2', 'Firefox/117.0.1', 'Firefox/116.0', 'Firefox/115.0.3', 'Firefox/114.0.2', 'Firefox/113.0.1', 'Firefox/112.0', 'Firefox/111.0.1', 'Firefox/110.0']

    def get_browser_stack(self, num_browsers: int=1) -> List[str]:
        """
        Get a valid combination of browser versions.

        How it works:
        1. Check if the number of browsers is supported.
        2. Randomly choose a combination of browsers.
        3. Iterate through the combination and add browser versions.
        4. Return the browser stack.

        Args:
            num_browsers: Number of browser specifications (1-3)

        Returns:
            List[str]: A list of browser versions.
        """
        if num_browsers not in self.browser_combinations:
            raise ValueError(f'Unsupported number of browsers: {num_browsers}')
        combination = random.choice(self.browser_combinations[num_browsers])
        browser_stack = []
        for browser in combination:
            if browser == 'chrome':
                browser_stack.append(random.choice(self.chrome_versions))
            elif browser == 'firefox':
                browser_stack.append(random.choice(self.firefox_versions))
            elif browser == 'safari':
                browser_stack.append(random.choice(self.safari_versions))
            elif browser == 'edge':
                browser_stack.append(random.choice(self.edge_versions))
            elif browser == 'gecko':
                browser_stack.append(random.choice(self.rendering_engines['gecko']))
            elif browser == 'webkit':
                browser_stack.append(self.rendering_engines['chrome_webkit'])
        return browser_stack

    def generate(self, device_type: Optional[Literal['desktop', 'mobile']]=None, os_type: Optional[str]=None, device_brand: Optional[str]=None, browser_type: Optional[Literal['chrome', 'edge', 'safari', 'firefox']]=None, num_browsers: int=3) -> str:
        """
        Generate a random user agent with specified constraints.

        Args:
            device_type: 'desktop' or 'mobile'
            os_type: 'windows', 'macos', 'linux', 'android', 'ios'
            device_brand: Specific device brand
            browser_type: 'chrome', 'edge', 'safari', or 'firefox'
            num_browsers: Number of browser specifications (1-3)
        """
        platform = self.get_random_platform(device_type, os_type, device_brand)
        components = ['Mozilla/5.0', platform]
        browser_stack = self.get_browser_stack(num_browsers)
        if 'Firefox' in str(browser_stack) or browser_type == 'firefox':
            components.append(random.choice(self.rendering_engines['gecko']))
        elif 'Chrome' in str(browser_stack) or 'Safari' in str(browser_stack) or browser_type == 'chrome':
            components.append(self.rendering_engines['chrome_webkit'])
            components.append('(KHTML, like Gecko)')
        elif 'Edge' in str(browser_stack) or browser_type == 'edge':
            components.append(self.rendering_engines['safari_webkit'])
            components.append('(KHTML, like Gecko)')
        elif 'Safari' in str(browser_stack) or browser_type == 'safari':
            components.append(self.rendering_engines['chrome_webkit'])
            components.append('(KHTML, like Gecko)')
        components.extend(browser_stack)
        return ' '.join(components)

    def generate_with_client_hints(self, **kwargs) -> Tuple[str, str]:
        """Generate both user agent and matching client hints"""
        user_agent = self.generate(**kwargs)
        client_hints = self.generate_client_hints(user_agent)
        return (user_agent, client_hints)

    def get_random_platform(self, device_type, os_type, device_brand):
        """Helper method to get random platform based on constraints"""
        platforms = self.desktop_platforms if device_type == 'desktop' else self.mobile_platforms if device_type == 'mobile' else {**self.desktop_platforms, **self.mobile_platforms}
        if os_type:
            for platform_group in [self.desktop_platforms, self.mobile_platforms]:
                if os_type in platform_group:
                    platforms = {os_type: platform_group[os_type]}
                    break
        os_key = random.choice(list(platforms.keys()))
        if device_brand and device_brand in platforms[os_key]:
            return platforms[os_key][device_brand]
        return random.choice(list(platforms[os_key].values()))

    def parse_user_agent(self, user_agent: str) -> Dict[str, str]:
        """Parse a user agent string to extract browser and version information"""
        browsers = {'chrome': 'Chrome/(\\d+)', 'edge': 'Edg/(\\d+)', 'safari': 'Version/(\\d+)', 'firefox': 'Firefox/(\\d+)'}
        result = {}
        for browser, pattern in browsers.items():
            match = re.search(pattern, user_agent)
            if match:
                result[browser] = match.group(1)
        return result

    def generate_client_hints(self, user_agent: str) -> str:
        """Generate Sec-CH-UA header value based on user agent string"""
        browsers = self.parse_user_agent(user_agent)
        hints = []
        if 'chrome' in browsers:
            hints.append(f'''"Chromium";v="{browsers['chrome']}"''')
            hints.append('"Not_A Brand";v="8"')
            if 'edge' in browsers:
                hints.append(f'''"Microsoft Edge";v="{browsers['edge']}"''')
            else:
                hints.append(f'''"Google Chrome";v="{browsers['chrome']}"''')
        elif 'firefox' in browsers:
            return '""'
        elif 'safari' in browsers:
            hints.append(f'''"Safari";v="{browsers['safari']}"''')
            hints.append('"Not_A Brand";v="8"')
        return ', '.join(hints)
