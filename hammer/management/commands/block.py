"""File mail whose header contains a value into a folder, via a ManageSieve rule."""

from logging import getLogger
from re import sub
from urllib.parse import unquote

from django.core.management.base import BaseCommand, CommandParser
from sievelib.managesieve import Client

from hammer.management.commands.hammer import quote
from hammer.management.commands.list_email import read_email_url

logger = getLogger(__name__)


def insert_rule(script: str, header: str, value: str, folder: str) -> str:
    """Return script with the rule first after its requires, unless already present."""
    rule = (
        f'if header :contains {quote(header)} {quote(value)} '
        f'{{ fileinto {quote(folder)}; stop; }}\n'
    )
    if rule in script:
        return script
    # Sieve demands require before any other command; repeating one is harmless
    body = sub(r'\A(?:\s*(?:#[^\n]*\n|require\s[^;]*;))*\s*', '', script, count=1)
    requires = len(script) - len(body)
    return f'require "fileinto";\n{script[:requires]}{rule}{body}'


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a header, a value it contains, and a folder."""
        parser.add_argument('header', help='e.g. Received')
        parser.add_argument('value', help='e.g. mta.exacttarget.com')
        parser.add_argument('--folder', default='Junk')

    def handle(
        self, *_args: object, folder: str, header: str, value: str, **_options: object
    ) -> None:
        """Insert the rule into the active Sieve script, or a new spamhammer one."""
        if not (parsed := read_email_url()):
            return
        client = Client(parsed.hostname or '')
        if not client.connect(
            unquote(parsed.username or ''),
            unquote(parsed.password or ''),
            authmech='PLAIN',
            starttls=True,
        ):
            raise ConnectionError(f'{parsed.hostname} ManageSieve login failed')
        try:
            active, _inactive = client.listscripts()
            name = active or 'spamhammer'
            script = client.getscript(name) if active else ''
            logger.info('Sieve %s before:\n%s', name, script)
            if (updated := insert_rule(script, header, value, folder)) == script:
                logger.info('Rule already present')
                return
            if not client.checkscript(updated):
                raise ValueError(f'Sieve rejected:\n{updated}')
            client.putscript(name, updated)
            if not active:
                client.setactive(name)
            logger.info('Sieve %s after:\n%s', name, updated)
        finally:
            client.logout()
