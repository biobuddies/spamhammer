"""Unsubscribe from a trusted sender using its advertised HTTPS one-click endpoint."""

from asyncio import run
from email import message_from_bytes, policy
from re import findall
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, CommandParser


def find_unsubscribe_url(raw: bytes) -> str:
    """Require unambiguous RFC 8058 headers and an HTTPS endpoint."""
    message = message_from_bytes(raw, policy=policy.default)
    unsubscribe = message.get_all('List-Unsubscribe', [])
    post = message.get_all('List-Unsubscribe-Post', [])
    if (
        len(unsubscribe) != 1
        or len(post) != 1
        or str(post[0]).strip() != 'List-Unsubscribe=One-Click'
    ):
        raise CommandError(
            'Email does not advertise one-click unsubscribe; inspect with peek_email'
        )
    urls = [
        url.strip()
        for url in findall(r'<([^<>]+)>', str(unsubscribe[0]))
        if urlsplit(url.strip()).scheme.lower() in {'http', 'https'}
    ]
    if len(urls) != 1:
        raise CommandError('Expected exactly one HTTPS unsubscribe URL')
    parsed = urlsplit(urls[0])
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CommandError('Expected an HTTPS unsubscribe URL without credentials or fragment')
    return urls[0]


async def unsubscribe(sender: str, subject: str, dry_run: bool) -> str | None:
    """POST once without cookies, credentials, or redirects; leave the email untouched."""
    from hammer.management.commands.peek_email import fetch_email  # noqa: PLC0415 breaks import cycle

    raw = await fetch_email(sender, subject)
    if raw is None:
        return None
    url = find_unsubscribe_url(raw)
    hostname = urlsplit(url).hostname
    if dry_run:
        return f'Would request unsubscribe for {sender} via {hostname}'
    try:
        async with (
            ClientSession(cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=30)) as session,
            session.post(
                url, data={'List-Unsubscribe': 'One-Click'}, allow_redirects=False
            ) as response,
        ):
            if not 200 <= response.status < 300:
                raise CommandError(f'Unsubscribe returned HTTP {response.status}')
    except (ClientError, TimeoutError):
        raise CommandError(
            'Unsubscribe request failed; inspect the sender with peek_email'
        ) from None
    return f'Unsubscribe accepted for {sender} by {hostname}'


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a trusted sender and optional subject substring."""
        parser.add_argument(
            'email', help='sender address, then optionally a space and subject substring'
        )
        parser.add_argument('--dry-run', action='store_true', help='check headers without posting')

    def handle(self, *_args: object, email: str, dry_run: bool, **_options: object) -> None:
        """Request one-click unsubscribe for the latest matching email."""
        sender, _, subject = email.partition(' ')
        if result := run(unsubscribe(sender, subject, dry_run)):
            self.stdout.write(result)
            call_command('junk_email', sender, dry_run=dry_run, stdout=self.stdout)
