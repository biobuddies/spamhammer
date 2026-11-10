"""Print each ManageSieve script, active first, read-only."""

from asyncio import run

from django.core.management.base import BaseCommand

from hammer.management.commands.hammer import quote
from hammer.managesieve import ManageSieve, sieve_session


@sieve_session(off=[])
async def show_scripts(sieve: ManageSieve) -> list[str]:
    """Return each script under a `# name (active)` header."""
    names = sorted(
        await sieve.command('LISTSCRIPTS'), key=lambda line: (line[1:] != ['ACTIVE'], line)
    )
    return [
        f'# {name}{" (active)" * bool(flags)}\n'
        + (await sieve.command('GETSCRIPT', quote(name)))[0][0]
        for name, *flags in names
    ]


class Command(BaseCommand):
    help = __doc__

    def handle(self, *_args: object, **_options: object) -> None:
        """Print scripts separated by blank lines."""
        if scripts := run(show_scripts()):
            print(*scripts, sep='\n')
