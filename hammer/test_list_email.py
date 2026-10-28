"""Exercise list_email without a mail server."""

from logging import INFO

from django.core.management import call_command
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises


@mark.parametrize('urls', ('', 'imaps://a:b@c, imaps://d:e@f', 'imaps://a:b@c imaps://d:e@f'))
def test_list_email_requires_one_url(monkeypatch: MonkeyPatch, urls: str) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', urls)
    with raises(NotImplementedError):
        call_command('list_email')


def test_list_email_defaults_off(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    with caplog.at_level(INFO):
        call_command('list_email')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text
