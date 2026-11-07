"""Owned Chromium resource modes, adapted from Crawl4AI browser_manager.py.

Source: 838869bdda93f25e6d50b3f50ae9577d9b19a586, Apache-2.0;
original license and attribution remain in licenses/ and upstream/.
The complete source light/text flag sets are retained. Light mode is restricted
to anonymous ephemeral contexts because its password/keychain and protection
flags must never reconfigure an existing account/profile. No performance gain
or anti-detection effectiveness is asserted.
"""
from dataclasses import dataclass
from fnmatch import fnmatchcase
from urllib.parse import urlsplit


LIGHT_FLAGS = (
    '--disable-background-networking', '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows', '--disable-breakpad',
    '--disable-client-side-phishing-detection',
    '--disable-component-extensions-with-background-pages', '--disable-default-apps',
    '--disable-extensions', '--disable-features=TranslateUI', '--disable-hang-monitor',
    '--disable-ipc-flooding-protection', '--disable-popup-blocking',
    '--disable-prompt-on-repost', '--disable-sync', '--force-color-profile=srgb',
    '--metrics-recording-only', '--no-first-run', '--password-store=basic',
    '--use-mock-keychain',
)
TEXT_FLAGS = (
    '--blink-settings=imagesEnabled=false', '--disable-remote-fonts',
    '--disable-images', '--disable-javascript', '--disable-software-rasterizer',
    '--disable-dev-shm-usage',
)
MEMORY_FLAGS = ('--aggressive-cache-discard', '--js-flags=--max-old-space-size=512')
CSS_EXTENSIONS = frozenset(('css', 'less', 'scss', 'sass'))
AD_PATTERNS = tuple('**/' + host + '/**' for host in (
    'google-analytics.com', 'googletagmanager.com', 'googlesyndication.com',
    'doubleclick.net', 'adservice.google.com', 'adsystem.com', 'adzerk.net',
    'adnxs.com', 'ads.linkedin.com', 'facebook.net', 'analytics.twitter.com',
    'ads-twitter.com', 'hotjar.com', 'clarity.ms', 'scorecardresearch.com',
    'pixel.wp.com', 'amazon-adsystem.com', 'mixpanel.com', 'segment.com'))
# Exact source extension inventory. Matching the parsed lower-case URL path is
# an intentional improvement over the upstream **/*.ext glob (query/case).
STATIC_EXTENSIONS = frozenset((
    'jpg jpeg png gif webp svg ico bmp tiff psd '
    'woff woff2 ttf otf eot '
    'mp4 webm ogg avi mov wmv flv m4v mp3 wav aac m4a opus flac '
    'pdf doc docx xls xlsx ppt pptx zip rar 7z tar gz xml swf wasm'
).split())


@dataclass(frozen=True)
class BrowserModes:
    text: bool
    light: bool
    css: bool = False
    ads: bool = False
    memory: bool = False

    @property
    def active(self):
        return self.text or self.light or self.css or self.ads or self.memory

    @property
    def flags(self):
        return tuple(dict.fromkeys((*(LIGHT_FLAGS if self.light else ()),
                                    *(TEXT_FLAGS if self.text else ()),
                                    *(MEMORY_FLAGS if self.memory else ()))))

    def blocks(self, request):
        path = urlsplit(request.url).path.lower()
        extension = path.rsplit('.', 1)[-1] if '.' in path else ''
        if self.css and extension in CSS_EXTENSIONS:
            return True
        if self.ads and any(fnmatchcase(request.url, pattern) for pattern in AD_PATTERNS):
            return True
        if not self.text:
            return False
        if request.resource_type in {'image', 'font', 'media'}:
            return True
        return extension in STATIC_EXTENSIONS

    def receipt(self):
        return {'text_mode': self.text, 'light_mode': self.light,
            'avoid_css': self.css, 'avoid_ads': self.ads, 'memory_saving_mode': self.memory,
            'source_commit': '838869bdda93f25e6d50b3f50ae9577d9b19a586',
            'applied_source_flags': list(self.flags), 'blocked_requests': 0,
            'page_javascript': 'disabled_by_context' if self.text else 'configured_context',
            'limitations': [
                'Only owned contexts with shared budgets; launch modes require Chromium',
                'Light mode source flags require anonymous ephemeral state',
                'Text mode blocks source extensions plus image/font/media resource types; query/case normalized',
                'Text mode disables page JavaScript explicitly; automation evaluation remains available',
                'Service workers blocked while a mode is active',
                'No measured performance improvement is implied',
                'avoid_ads retains source URL globs, not a comprehensive or semantic domain blocklist',
                'memory_saving_mode limits V8 old-space to 512 MB; large scripts may fail',
            ]}


def browser_modes(params, *, engine, manage_budget, account, storage_state, context_options):
    for field in ('text_mode', 'light_mode', 'avoid_css', 'avoid_ads', 'memory_saving_mode'):
        if type(params.get(field, False)) is not bool:
            raise ValueError(field + ' must be boolean')
    modes = BrowserModes(*(params.get(key, False) for key in
        ('text_mode', 'light_mode', 'avoid_css', 'avoid_ads', 'memory_saving_mode')))
    if not modes.active:
        return modes
    if ((modes.text or modes.light or modes.memory) and engine != 'chromium') or params.get('cdp_url') or not manage_budget:
        raise ValueError('Resource modes require owned contexts and shared budgets; launch modes require Chromium')
    if modes.light and (account or params.get('browser_profile') or storage_state is not None):
        raise ValueError('light_mode source password/keychain flags require anonymous ephemeral browsing')
    if modes.text:
        for field in ('java_script_enabled', 'has_touch', 'is_mobile'):
            if field in context_options and context_options[field] is not False:
                raise ValueError('text_mode conflicts with browser context option ' + field)
    return modes
