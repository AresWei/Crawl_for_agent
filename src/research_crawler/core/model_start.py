"""Coordinated Windows launcher; no second lock inside its PowerShell child."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import time

import httpx

from .gpu import GPUCoordinator, _settle, _verified, _stop
from .model_profiles import installed_profile, state_profile


async def _wait_existing(server, identity: dict, wait_seconds: float) -> int:
    token = (server.runtime / 'token').read_text(encoding='utf-8-sig').strip()
    deadline = time.monotonic() + wait_seconds
    async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
        while time.monotonic() < deadline:
            if _verified(identity) is None:
                raise RuntimeError('Owned model worker exited during startup')
            try:
                response = await client.get('http://127.0.0.1:8947/health',
                    headers={'Authorization': 'Bearer ' + token})
                if response.status_code == 200:
                    print(f'Local model ready at http://127.0.0.1:8947 (PID {identity["pid"]}).')
                    return 0
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.25)
    print(f'Owned model PID {identity["pid"]} is still loading; it was left running.')
    return 2


async def start(root: Path, *, cpu: bool = False, wait_seconds: int = 240, model_id: str | None = None) -> int:
    if not 5 <= wait_seconds <= 360:
        raise ValueError('wait_seconds must be between 5 and 360')
    coordinator = GPUCoordinator(root)
    selected = installed_profile(root,model_id) if model_id is not None else None
    # An explicit selection must not first reload a parked different model.
    async with coordinator.lease('language', restore_timeout=wait_seconds, restore_language=model_id is None):
        owned = coordinator.language._owned()
        if selected and owned and state_profile(root,owned[0])['id']!=selected['id']:
            await _settle(asyncio.to_thread(coordinator.language.park))
            owned=None
        if owned is not None:
            # This includes a loading process that has not bound its port yet.
            # Never start another copy just because the listener is not ready.
            code=await _wait_existing(coordinator.language, owned[1], wait_seconds)
            if selected and code==0:
                coordinator.language.parked_path.unlink(missing_ok=True)
            return code
        coordinator.language._check_port()
        shell = shutil.which('pwsh') or shutil.which('powershell')
        if shell is None:
            raise RuntimeError('PowerShell is required for the Windows model launcher')
        command = [shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(root / 'scripts/model-start.ps1'), '-GpuLeaseHeld', '-WaitSeconds', str(wait_seconds)]
        if cpu:
            command.append('-Cpu')
        if selected:
            command.extend(['-Model',selected['id']])
        environment = coordinator.paths.child_environment()
        environment['RESEARCH_CRAWLER_GPU_LAUNCH_OWNER'] = str(os.getpid())
        # Cancellation cannot release the GPU while the child is still launching.
        result = await _settle(asyncio.to_thread(subprocess.run, command, env=environment,
            cwd=root, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
        if selected and result.returncode == 0:
            current=coordinator.language._owned()
            if current is None or state_profile(root,current[0])['id']!=selected['id']:
                raise RuntimeError('Selected model startup did not establish its owned identity')
            coordinator.language.parked_path.unlink(missing_ok=True)
        elif selected and result.returncode not in {0,2} and coordinator.language.parked_path.exists():
            # Failed switch: stop only our verified candidate and restore the
            # original saved worker; no CPU fallback or second resident server.
            current=coordinator.language._owned()
            if current is not None:await _settle(asyncio.to_thread(_stop,current[1]))
            await _settle(coordinator.language.restore(wait_seconds=wait_seconds))
        return result.returncode
