"""Parse RFC 8058 one-click unsubscribe headers."""

from email import message_from_bytes, policy
from re import findall
from urllib.parse import urlsplit

from django.core.management.base import CommandError


def find_unsubscribe_url(raw: bytes) -> str:
    """Require unambiguous RFC 8058 headers and one HTTP(S) endpoint."""
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
    # HTTP instead of HTTPS is non-compliant, but if it works let's use it. TODO circle back once we
    # have data on whether a sufficient percentage of HTTP links actually work.
    urls = [
        url.strip()
        for url in findall(r'<([^<>]+)>', str(unsubscribe[0]))
        if urlsplit(url.strip()).scheme.lower() in {'http', 'https'}
    ]
    if len(urls) != 1:
        raise CommandError('Expected exactly one HTTP(S) unsubscribe URL')
    parsed = urlsplit(urls[0])
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CommandError('Expected an unsubscribe URL without credentials or fragment')
    return urls[0]
