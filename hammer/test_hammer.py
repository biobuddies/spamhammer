"""Exercise hammer's graceful degradation without network access."""

from logging import INFO

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises


@mark.parametrize('key', ('off', 'off until I find it'))
def test_hammer_off(caplog: LogCaptureFixture, key: str, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    monkeypatch.setenv('SPAMHAUS_API_KEY', key)
    with caplog.at_level(INFO):
        call_command('hammer', 'spam@example.com')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text
    assert 'SPAMHAUS_API_KEY off' in caplog.text


@mark.parametrize(
    ('spam', 'logged'),
    (
        ('spam@example.com', "spam@example.com ''"),
        ('spam@example.com Act now', "spam@example.com 'Act now'"),
        ('spam@example.com  Re: Act now ', "spam@example.com ' Re: Act now '"),
    ),
)
def test_hammer_spam(caplog: LogCaptureFixture, logged: str, spam: str) -> None:
    with caplog.at_level(INFO):
        call_command('hammer', spam)
    assert f'Hammering {logged}' in caplog.text


def test_hammer_rejects_empty_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    monkeypatch.setenv('SPAMHAUS_API_KEY', '')
    with raises(ImproperlyConfigured):
        call_command('hammer', 'spam@example.com')
