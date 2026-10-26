"""Exercise list_email argument handling without a mail server."""

from django.core.management import call_command
from pytest import MonkeyPatch, mark, raises


@mark.parametrize('urls', ('', 'imaps://a:b@c, imaps://d:e@f', 'imaps://a:b@c imaps://d:e@f'))
def test_list_email_requires_one_url(monkeypatch: MonkeyPatch, urls: str) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', urls)
    with raises(NotImplementedError):
        call_command('list_email')
