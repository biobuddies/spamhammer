"""File mail whose header contains a value into a folder, via a ManageSieve rule."""

from asyncio import run
from collections import Counter
from email import message_from_bytes, policy
from email.utils import parseaddr
from logging import getLogger
from re import sub

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.management.commands.hammer import quote
from hammer.management.commands.list_email import imap_session
from hammer.managesieve import ManageSieve, sieve_session

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


@sieve_session()
async def update_script(sieve: ManageSieve, header: str, value: str, folder: str) -> None:
    """Insert the rule into the active Sieve script, or a new spamhammer one."""
    active = next((name for name, *flags in await sieve.listscripts() if flags == ['ACTIVE']), None)
    name = active or 'spamhammer'
    script = (await sieve.getscript(quote(name)))[0][0] if active else ''
    logger.info('Sieve %s before:\n%s', name, script)
    if (updated := insert_rule(script, header, value, folder)) == script:
        logger.info('Rule already present')
        return
    # Non-synchronizing literal, which RFC 5804 servers must accept
    literal = f'{{{len(updated.encode())}+}}\r\n{updated}'
    await sieve.checkscript(literal)
    await sieve.putscript(quote(name), literal)
    if not active:
        await sieve.setactive(quote(name))
    logger.info('Sieve %s after:\n%s', name, updated)


@imap_session(off=True)
async def folder_exists(imap: IMAP4_SSL, folder: str) -> bool:
    """LIST the exact folder; responses before the tagged OK name each match."""
    listed = await imap.list('""', quote(folder))
    return any(line.startswith(b'(') for line in listed.lines if isinstance(line, bytes))


@imap_session(off=Counter())
async def count_matches(imap: IMAP4_SSL, header: str, value: str) -> Counter[str]:
    """Count inbox senders whose header contains value, as the rule would match."""
    await imap.select('INBOX')
    numbers = (await imap.search('HEADER', quote(header), quote(value))).lines[0].decode().split()
    lines = (
        (await imap.fetch(','.join(numbers), 'BODY.PEEK[HEADER.FIELDS (FROM)]')).lines
        if numbers
        else []
    )
    return Counter(
        parseaddr(message_from_bytes(line, policy=policy.default)['From'])[1]
        for line in lines
        if isinstance(line, bytearray)
    )


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a header, a value it contains, and a folder."""
        parser.add_argument('header', help='e.g. Received')
        parser.add_argument('value', help='e.g. mta.exacttarget.com')
        parser.add_argument('--backtest', action='store_true', help='count INBOX matches only')
        parser.add_argument('--folder', default='Spam', help='must already exist')

    def handle(
        self,
        *_args: object,
        backtest: bool,
        folder: str,
        header: str,
        value: str,
        **_options: object,
    ) -> None:
        """Count matches, or insert the rule."""
        if backtest:
            senders = run(count_matches(header, value)) or Counter()
            print(
                f'{senders.total()} INBOX messages',
                *(f'{count} {sender}' for sender, count in senders.most_common()),
                sep='\n',
            )
            return
        if not run(folder_exists(folder)):
            raise CommandError(f'No {folder} folder; Sieve would keep matches in INBOX')
        run(update_script(header, value, folder))
