"""Image jobs use the shared runner, checkpoints and global GPU lease."""
from ..creation.engine import create


def register(runner):
    async def generate(context):
        return await create(context, "generate")

    async def edit(context):
        return await create(context, "edit")

    runner.register("creation.generate", generate)
    runner.register("creation.edit", edit)
