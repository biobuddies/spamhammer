"""Print each ManageSieve script, active first, read-only."""

from django.core.management.base import BaseCommand
from sievelib.managesieve import Client

from hammer.management.commands.block import sieve_session


@sieve_session(off=[])
def show_scripts(client: Client) -> list[str]:
    """Return each script under a `# name (active)` header."""
    active, inactive = client.listscripts()
    return [
        f'# {name}{" (active)" if name == active else ""}\n{client.getscript(name)}'
        for name in ([active] if active else []) + sorted(inactive)
    ]


class Command(BaseCommand):
    help = __doc__

    def handle(self, *_args: object, **_options: object) -> None:
        """Print scripts separated by blank lines."""
        if scripts := show_scripts():
            print(*scripts, sep='\n')
