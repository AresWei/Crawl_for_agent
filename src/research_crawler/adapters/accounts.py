from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from ..core.accounts import AccountStore, PLATFORM_SITES, matches_domain
from ..core.jobs import NeedsLogin
from ..core.sessions import controlled, open_browser
from ..core.exclusive import lease


CAPABILITIES = {"implemented": ["normal_browser_login", "selected_account_cache", "windows_dpapi", "session_revocation",
                               "scoped_cookiebridge", "explicit_session_check", "post_login_validation", "revision_bound_validity"],
                "pending": ["authenticated_platform_live_acceptance", "dy_tieba_remote_authentication_probe",
                            "xhs_ks_unambiguous_authentication_indicator"]}


async def check_session(context,*,validator=None,expected_revision=None):
    from ..platforms.session_validation import validate_session
    platform=context.params['platform'];account=context.params.get('account','main')
    store=AccountStore(context.paths)
    await lease(context,'account-checks',(platform,account))
    revision=await asyncio.to_thread(store.validation_revision,platform,account)
    result=None
    if expected_revision is None or expected_revision==revision:
        try:
            await asyncio.to_thread(store.state,platform,account,required=True)
        except NeedsLogin:
            result={'state':'auth_required','reason_code':'saved_state_missing_or_expired','method':'local_saved_state'}
        else:
            if platform in {'web','reddit'} and validator is None:
                result={'state':'indeterminate','reason_code':'no_platform_authentication_probe','method':'local_saved_state'}
            else:
                result=await controlled(context,(validator or validate_session)(context,platform,account))
    await context.check_control()
    info=await asyncio.to_thread(store.record_validation,platform,account,result,expected_revision=revision) if result is not None else None
    if info is None:
        summary={'status':'partial','platform':platform,'account':account,'session_validity':'indeterminate',
                 'reason':'session_changed_during_check','message':'会话在校验期间已更改，请校验当前保存的会话。'}
    else:
        validity=info['validation']['state']
        summary={'status':'complete' if validity=='valid' else 'partial','platform':platform,'account':account,
                 'session_validity':validity,'validation':info['validation'],
                 'message':{'valid':'平台已接受本次校验所用会话；后续访问仍可能需要重新登录。',
                            'auth_required':'保存的会话未通过认证，请在所选平台重新登录。',
                            'indeterminate':'暂时无法确认会话是否有效，保留已保存状态。'}[validity]}
    saved=await context.checkpoint('account_validation')
    await context.commit_page('account_validation',[],next_cursor=None,exhausted=summary['status']=='complete',
        expected_revision=saved['revision'] if saved else 0,metadata={'summary':summary})
    if info is not None and summary['session_validity']=='auth_required':
        raise NeedsLogin(platform,account)
    return summary


class LoginManager:
    def __init__(self):
        self.waiting: dict[str, asyncio.Event] = {}

    def confirm(self, job_id: str) -> None:
        if job_id not in self.waiting:
            raise KeyError(job_id)
        self.waiting[job_id].set()

    async def login(self, context):
        platform = context.params["platform"]
        account = context.params.get("account", "main")
        store = AccountStore(context.paths)
        store.create(platform, account, domains=context.params.get("domains"))
        target = PLATFORM_SITES[platform][0] if platform in PLATFORM_SITES else "https://" + store.domains(platform, account)[0]
        if context.params.get("login_url"):
            selected = urlsplit(context.params["login_url"])
            if selected.scheme != "https" or not matches_domain(selected.hostname or "", store.domains(platform, account)) or selected.username:
                raise ValueError("Login URL must belong to the selected platform")
            target = context.params["login_url"]
        try:
            state = store.state(platform, account)
        except NeedsLogin:
            state = {"cookies": [], "origins": []}
        event = asyncio.Event()
        self.waiting[context.job_id] = event
        try:
            session = await open_browser(context, platform=platform, account=account,
                headless=False, storage_state=state, manage_budget=False, block_media=False)
            await controlled(context, session.page.goto(target, wait_until="domcontentloaded", timeout=45000))
            deadline = max(30, min(1800, int(context.params.get("login_seconds", 300))))
            try:
                await asyncio.wait_for(controlled(context, event.wait()), timeout=deadline)
            except TimeoutError:
                raise NeedsLogin(platform, account, "Login was not confirmed before the deadline")
            saved=await asyncio.to_thread(store.save,platform,account,await session.context.storage_state())
            if platform not in {'web','reddit'}:
                return await check_session(context,expected_revision=saved['session_revision'])
            return {"status":"partial","platform":platform,"account":account,"session_validity":"saved_unverified",
                    "message":"会话已保存；该站点尚无专用远端认证校验。"}
        finally:
            self.waiting.pop(context.job_id, None)


def register(runner):
    manager = LoginManager()
    runner.login_manager = manager
    runner.register("account.login", manager.login)
    runner.register('account.check',check_session)
