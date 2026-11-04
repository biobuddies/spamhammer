"""Exercise abuse reports without a mail server."""

from email import message_from_bytes, policy
from logging import INFO
from unittest.mock import MagicMock

from pytest import LogCaptureFixture, MonkeyPatch, mark

from hammer import abuse
from hammer.abuse import PLATFORMS, compose_report, email_reports, match_platforms

SALESFORCE = PLATFORMS[0]
SPAM = (
    b'Received: from mta.s7.exacttarget.com (mta.s7.exacttarget.com. [13.111.4.1])\r\n'
    b'From: MileagePlus <partner@enews.united.com>\r\n'
    b'Subject: NEW OFFER\r\n'
    b'\r\n'
    b'Earn bonus miles'
)


@mark.parametrize(
    ('header', 'platforms'),
    (
        (b'Received: from mta.s7.exacttarget.com', [SALESFORCE]),
        (b'Return-Path: <bounce-1_HTML-2@bounce.s7.EXCT.NET>', [SALESFORCE]),
        (b'DKIM-Signature: v=1; a=rsa-sha256; d=exacttarget.com; s=200608', [SALESFORCE]),
        (b'Received: from mta.notexacttarget.com', []),
        (b'From: <spam@exacttarget.com>', []),
    ),
)
def test_match_platforms(header: bytes, platforms: list) -> None:
    assert match_platforms(header + b'\r\n\r\nAct now') == platforms


def test_compose_report() -> None:
    serialized = compose_report('me@example.com', SALESFORCE, SPAM).as_bytes()
    report = message_from_bytes(serialized, policy=policy.default)
    assert (report['From'], report['To'], report['Subject']) == (
        'me@example.com',
        'abuse@salesforce.com',
        'Abuse report: NEW OFFER',
    )
    assert report.get_content_type() == 'multipart/report'
    assert report.get_param('report-type') == 'feedback-report'
    assert [part.get_content_type() for part in report.iter_parts()] == [
        'text/plain',
        'message/feedback-report',
        'message/rfc822',
    ]
    assert b'Feedback-Type: abuse\nUser-Agent: spamhammer\nVersion: 1\n' in serialized
    assert b'Received: from mta.s7.exacttarget.com' in serialized


@mark.parametrize(
    ('submission', 'login_pair'),
    (
        ('submission://smtp.example.com', ('me@example.com', 'secret')),
        ('submission://you%40example.com:other@smtp.example.com', ('you@example.com', 'other')),
    ),
)
def test_email_reports(
    login_pair: tuple[str, str], monkeypatch: MonkeyPatch, submission: str
) -> None:
    monkeypatch.setattr(abuse, 'SMTP', smtp_class := MagicMock())
    monkeypatch.setenv(
        'SPAMHAMMER_EMAIL_URLS', f'imaps://me%40example.com:secret@imap.example.com, {submission}'
    )
    email_reports(SPAM)
    smtp_class.assert_called_once_with('smtp.example.com', 587, timeout=30)
    smtp = smtp_class.return_value.__enter__.return_value
    smtp.login.assert_called_once_with(*login_pair)
    assert [
        (call.args[0]['From'], call.args[0]['To']) for call in smtp.send_message.call_args_list
    ] == [('me@example.com', 'abuse@salesforce.com')]


def test_email_reports_needs_submission(
    caplog: LogCaptureFixture, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://me:secret@imap.example.com')
    with caplog.at_level(INFO):
        email_reports(SPAM)
    assert 'No submission:// URL; skipped Salesforce Marketing Cloud' in caplog.text


def test_email_reports_unknown_platform(caplog: LogCaptureFixture) -> None:
    with caplog.at_level(INFO):
        email_reports(b'Received: from mail.example.com\r\n\r\nHi')
    assert 'No known platform sent it' in caplog.text
