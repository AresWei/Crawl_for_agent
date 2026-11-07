"""Explicit ordinary-page behavior from Crawl4AI 838869b (Apache-2.0).

Sources: js_snippet/navigator_overrider.js; async_crawler_strategy.py:1011.
No challenge solving or claim of detection resistance. Defaults are unchanged.
"""
import random

NAVIGATOR_SCRIPT = r'''(() => {
const originalQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
window.navigator.permissions.query = (parameters) =>
    parameters.name === "notifications"
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);
Object.defineProperty(navigator, "webdriver", { get: () => undefined });
window.navigator.chrome = { runtime: {} };
Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
Object.defineProperty(document, "hidden", { get: () => false });
Object.defineProperty(document, "visibilityState", { get: () => "visible" });
})();'''


def behavior_options(params):
    for key in ('override_navigator', 'simulate_user', 'magic'):
        if type(params.get(key, False)) is not bool:
            raise ValueError(key + ' must be boolean')
    active = any(params.get(key) for key in ('override_navigator', 'simulate_user', 'magic'))
    if active and params.get('cdp_url'):
        raise ValueError('Navigator and interaction options require an owned context')
    return active


def magic_user_agent(params):
    """Expand source magic UA before shared selection/checkpoint hashing."""
    behavior_options(params)
    if not params.get('magic') or params.get('browser_profile'):
        return params
    explicit = params.get('user_agent') or params.get('browser_options', {}).get('user_agent') or any(
        k.lower() == 'user-agent' for k in params.get('headers', {}))
    if not explicit and params.get('user_agent_mode', 'explicit') == 'explicit':
        params = {**params, 'user_agent_mode': 'random'}
    return params


async def simulate_interaction(page, context):
    points = [[random.randint(100, 300), random.randint(150, 300)],
              [random.randint(300, 600), random.randint(200, 400)]]
    wheel = random.randint(200, 400)
    for x, y in points:
        await context.check_control()
        await page.mouse.move(x, y)
    await context.check_control()
    await page.mouse.wheel(0, wheel)
    return {'moves': points, 'wheel': [0, wheel], 'clicks': 0, 'keyboard_events': 0,
            'scope': 'ordinary_page_input; no detection-resistance claim'}


def navigator_receipt(params):
    if not behavior_options(params):
        return None
    return {'enabled': True, 'source': 'crawl4ai-navigator-overrider',
            'limitations': ['plugins is a numeric stub, not a native PluginArray',
                           'languages hardcoded en-US/en independently of locale',
                           'visibility forced visible; not actual foreground state'],
            'source_fix': 'bind non-notification permission query to its native receiver'}
