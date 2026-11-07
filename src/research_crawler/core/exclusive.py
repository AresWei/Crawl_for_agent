"""Application-home scoped exclusive resources, shared by jobs and CLI processes."""
import asyncio
import errno
import hashlib
import os

from .paths import safe_name


async def lease(context, namespace, identity):
    folder = context.paths.root / 'state' / safe_name(namespace)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (hashlib.sha256(str(identity).encode()).hexdigest() + '.lock')
    handle = target.open('a+b')
    try:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        while True:
            await context.check_control()
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                await asyncio.sleep(.1)
            else:
                context.resources.callback(handle.close)
                return
    except BaseException:
        handle.close()
        raise
