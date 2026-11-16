"""Exercise abuse reports without a mail server."""

from email import message_from_bytes, policy
from logging import INFO
from pathlib import Path
from unittest.mock import MagicMock

from pytest import LogCaptureFixture, MonkeyPatch, mark

from hammer import abuse
from hammer.abuse import PLATFORMS, Platform, compose_report, email_reports, match_platforms

SALESFORCE = PLATFORMS[0]
SPAM = (Path(__file__).parent / 'spam' / 'thealphapaths.eml').read_bytes()


@mark.parametrize(
    ('header', 'platforms'),
    (
        (
            (
                b'Received: from alv174.mta.exacttarget.com (alv174.mta.exacttarget.com'
                b' [159.92.166.174])'
            ),
            [SALESFORCE],
        ),
        (b'Received: by alv174.mta.exacttarget.com id hu6r0i39mjc3', [SALESFORCE]),
        (
            (
                b'DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/relaxed; s=fbldkim51;'
                b' d=s51.y.mc.salesforce.com;'
            ),
            [SALESFORCE],
        ),
        (
            (
                b'Return-Path: <bounce-UPMZ00ZQF2TXGHQQZMSTGQBR8L.510008'
                b'@bounce.news.thealphapaths.com>'
            ),
            [],
        ),
        (b'Received: from alv174.mta.notexacttarget.com', []),
        (b'From: "The Alpha Paths" <contact@exacttarget.com>', []),
    ),
)
def test_match_platforms(header: bytes, platforms: list[Platform]) -> None:
    assert match_platforms(header + b'\r\n\r\nAct now') == platforms


def test_compose_report() -> None:
    serialized = compose_report('busybuddy@biobuddi.es', SALESFORCE, SPAM).as_bytes()
    report = message_from_bytes(serialized, policy=policy.default)
    assert (report['From'], report['To'], report['Subject']) == (
        'busybuddy@biobuddi.es',
        'abuse@salesforce.com',
        'Abuse report: 83 parcels. One company.',
    )
    assert report.get_content_type() == 'multipart/report'
    assert report.get_param('report-type') == 'feedback-report'
    assert [part.get_content_type() for part in report.iter_parts()] == [
        'text/plain',
        'message/feedback-report',
        'message/rfc822',
    ]
    assert b'Feedback-Type: abuse\nUser-Agent: spamhammer\nVersion: 1\n' in serialized
    assert b'Received: from alv174.mta.exacttarget.com' in serialized


@mark.parametrize(
    ('submission', 'login_pair'),
    (
        ('submission://smtp.biobuddi.es', ('busybuddy@biobuddi.es', 'secret')),
        ('submission://buddy%40biobuddi.es:other@smtp.biobuddi.es', ('buddy@biobuddi.es', 'other')),
    ),
)
def test_email_reports(
    login_pair: tuple[str, str], monkeypatch: MonkeyPatch, submission: str
) -> None:
    monkeypatch.setattr(abuse, 'SMTP', smtp_class := MagicMock())
    monkeypatch.setenv(
        'SPAMHAMMER_EMAIL_URLS',
        f'imaps://busybuddy%40biobuddi.es:secret@imap.biobuddi.es, {submission}',
    )
    email_reports(SPAM)
    smtp_class.assert_called_once_with('smtp.biobuddi.es', 587, timeout=30)
    smtp = smtp_class.return_value.__enter__.return_value
    smtp.login.assert_called_once_with(*login_pair)
    assert [
        (call.args[0]['From'], call.args[0]['To']) for call in smtp.send_message.call_args_list
    ] == [('busybuddy@biobuddi.es', 'abuse@salesforce.com')]


def test_email_reports_needs_submission(
    caplog: LogCaptureFixture, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://busybuddy:secret@imap.biobuddi.es')
    with caplog.at_level(INFO):
        email_reports(SPAM)
    assert 'No submission:// URL; skipped Salesforce Marketing Cloud' in caplog.text


def test_email_reports_unknown_platform(caplog: LogCaptureFixture) -> None:
    with caplog.at_level(INFO):
        email_reports(b'Received: from mail.biobuddi.es\r\n\r\nHi')
    assert 'No known platform sent it' in caplog.text
