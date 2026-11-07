"""Visual analysis shares the normal persisted JobRunner and GPU queue."""
from ..vision.analysis import analyze

def register(runner):
    runner.register('media.vision', analyze)
