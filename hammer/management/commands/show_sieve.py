"""Print each ManageSieve script, active first, read-only."""

from asyncio import run

from django.core.management.base import BaseCommand

from hammer.managesieve import ManageSieve, sieve_session


@sieve_session(off=[])
async def show_scripts(sieve: ManageSieve) -> list[str]:
    """Return each script under a `# name (active)` header."""
    active, inactive = await sieve.list_scripts()
    return [
        f'# {name}{" (active)" if name == active else ""}\n{await sieve.get_script(name)}'
        for name in ([active] if active else []) + sorted(inactive)
    ]


class Command(BaseCommand):
    help = __doc__

    def handle(self, *_args: object, **_options: object) -> None:
        """Print scripts separated by blank lines."""
        if scripts := run(show_scripts()):
            print(*scripts, sep='\n')
