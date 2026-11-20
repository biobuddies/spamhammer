"""File mail whose header contains a value into a folder, via a ManageSieve rule."""

from asyncio import run
from itertools import batched
from logging import getLogger
from re import sub

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.hammer import ManageSieve, imap_session, sieve_session

logger = getLogger(__name__)


def insert_rule(script: str, header: str, value: str, folder: str) -> str:
    """Return script with the rule first after its requires, unless already present."""
    rule = 'if header :contains "%s" "%s" { fileinto "%s"; stop; }\n' % (header, value, folder)
    if rule in script:
        return script
    # Sieve demands require before any other command; repeating one is harmless
    body = sub(r'\A(?:\s*(?:#[^\n]*\n|require\s[^;]*;))*\s*', '', script, count=1)
    requires = len(script) - len(body)
    return f'require "fileinto";\n{script[:requires]}{rule}{body}'


@sieve_session()
async def update_script(sieve: ManageSieve, header: str, value: str, folder: str) -> None:
    """Insert the rule into the active Sieve script, or a new spamhammer one."""
    active = next((name for name, *args in await sieve.listscripts() if args == ['ACTIVE']), None)
    name = active or 'spamhammer'
    script = (await sieve.getscript(f'"{name}"'))[0][0] if active else ''
    logger.info('Sieve %s before:\n%s', name, script)
    if (updated := insert_rule(script, header, value, folder)) == script:
        logger.info('Rule already present')
        return
    # Non-synchronizing literal, which RFC 5804 servers must accept
    literal = f'{{{len(updated.encode())}+}}\r\n{updated}'
    await sieve.checkscript(literal)
    await sieve.putscript(f'"{name}"', literal)
    if not active:
        await sieve.setactive(f'"{name}"')
    logger.info('Sieve %s after:\n%s', name, updated)


@imap_session()
async def check_folder(imap: IMAP4_SSL, folder: str) -> None:
    """Check the exact folder, without treating wildcard characters as a LIST pattern."""
    await imap.status(f'"{folder}"', '(UIDVALIDITY)')


async def filter_matches(
    imap: IMAP4_SSL, header: str, value: str, folder: str, *, dry_run: bool
) -> int:
    """Preview inbox matches or install the filter and move the matching mail."""
    destination = f'"{folder}"'
    await imap.status(destination, '(UIDVALIDITY)')
    await imap.select('INBOX')
    if not dry_run:
        await update_script(header, value, folder)
    found = await imap.uid_search('HEADER', f'"{header}"', f'"{value}"')
    numbers = found.lines[0].decode().split()
    if not dry_run:
        for batch in batched(numbers, 500):
            try:
                await imap.uid('MOVE', ','.join(batch), destination)
            except CommandError as error:
                raise CommandError(f'{error} after installing Sieve rule') from error
    return len(numbers)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a header, a value it contains, and a folder."""
        parser.add_argument('header', help='e.g. Received')
        parser.add_argument('value', help='e.g. mta.exacttarget.com')
        parser.add_argument('--dry-run', action='store_true', help='count INBOX matches only')
        parser.add_argument('--folder', default='Spam', help='must already exist')
        parser.add_argument(
            '--move-existing', action='store_true', help='also move matching INBOX mail'
        )

    def handle(
        self,
        *_args: object,
        dry_run: bool,
        folder: str,
        header: str,
        move_existing: bool,
        value: str,
        **_options: object,
    ) -> None:
        """Count matches, or insert the rule."""
        run(
            self.handle_async(
                header, value, dry_run=dry_run, folder=folder, move_existing=move_existing
            )
        )

    async def handle_async(
        self, header: str, value: str, *, dry_run: bool, folder: str, move_existing: bool
    ) -> None:
        """Check the destination and update the Sieve script in one event loop."""
        if dry_run or move_existing:
            checked_filter = imap_session(requires=('MOVE',) if move_existing else ())(
                filter_matches
            )
            count = await checked_filter(header, value, folder, dry_run=dry_run)
            if dry_run:
                print(f'{count or 0} matching INBOX messages')
            elif count is not None:
                print(f'Moved {count} INBOX messages to {folder}')
            return
        await check_folder(folder)
        await update_script(header, value, folder)
