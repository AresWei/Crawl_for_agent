"""Small checkout bootstrap for the coordinated model-start PowerShell entry."""
import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from research_crawler.core.model_start import start


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cpu', action='store_true')
    parser.add_argument('--wait-seconds', type=int, default=240)
    parser.add_argument('--model', choices=['qwen3-4b','qwen3-8b','qwen3-4b-instruct-2507'], default='qwen3-4b')
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(start(ROOT, cpu=args.cpu, wait_seconds=args.wait_seconds, model_id=args.model)))
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
