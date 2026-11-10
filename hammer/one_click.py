"""Parse RFC 8058 one-click unsubscribe headers."""

from email import message_from_bytes, policy
from re import findall
from urllib.parse import urlsplit

from django.core.management.base import CommandError


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
