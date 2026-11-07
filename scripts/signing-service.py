"""Foreground same-product computation service; stop this process to stop service."""
import argparse
import os
import secrets
from research_crawler.core.paths import AppPaths
from research_crawler.signing.service import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=None)
    parser.add_argument('--port', type=int, default=8950)
    args = parser.parse_args()
    paths = AppPaths.discover(args.root)
    paths.initialize()
    token_file = paths.root / 'state/signing/token'
    token_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
    token = token_file.read_text().strip()
    import uvicorn
    uvicorn.run(create_app(paths, token), host='127.0.0.1', port=args.port,
                access_log=False, log_level='warning', timeout_graceful_shutdown=20)


if __name__ == '__main__':
    main()
