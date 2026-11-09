"""Print each ManageSieve script, active first, read-only."""

from django.core.management.base import BaseCommand

from hammer.management.commands.block import sieve_client


class Command(BaseCommand):
    help = __doc__

    def handle(self, *_args: object, **_options: object) -> None:
        """Print `# name (active)` headers, each followed by its script."""
        with sieve_client() as client:
            if not client:
                return
            active, inactive = client.listscripts()
            print(  # noqa: T201 stdout is the interface
                *(
                    f'# {name}{" (active)" if name == active else ""}\n{client.getscript(name)}'
                    for name in ([active] if active else []) + sorted(inactive)
                ),
                sep='\n',
            )
