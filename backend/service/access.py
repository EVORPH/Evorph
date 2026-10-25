import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import urlsplit

from evorph.contracts import InputError, exact, identifier, integer, parse_json


class AccessError(PermissionError):
    pass


def credential_digest(token):
    if type(token) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise AccessError('Request credentials are unavailable.')
    raw = base64.urlsafe_b64decode(token + '=')
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip('=') != token:
        raise AccessError('Request credentials are unavailable.')
    return hashlib.sha256(raw).hexdigest()


def issue_session(owner_id, *, now, lifetime_seconds):
    identifier(owner_id)
    integer(now, 0, 2**53 - 1)
    integer(lifetime_seconds, 1, 2**31 - 1)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    return {'token': token, 'csrf': csrf}, {'owner_id': owner_id, 'token_digest': credential_digest(token),
            'csrf_digest': credential_digest(csrf), 'expires_at': now + lifetime_seconds, 'revoked': False}


def authenticate(token, session, now):
    if session is None:
        raise AccessError('Request credentials are unavailable.')
    exact(session, {'owner_id', 'token_digest', 'csrf_digest', 'expires_at', 'revoked'})
    identifier(session['owner_id'])
    integer(now, 0, 2**53 - 1)
    integer(session['expires_at'], 0, 2**53 - 1)
    if (session['revoked'] is not False or now >= session['expires_at']
            or type(session['token_digest']) is not str
            or not hmac.compare_digest(credential_digest(token), session['token_digest'])):
        raise AccessError('Request credentials are unavailable.')
    return session['owner_id']


def authorize_write(token, session, *, now, origin, expected_origin, csrf):
    owner = authenticate(token, session, now)
    if type(expected_origin) is not str:
        raise InputError('Service origin is not configured.')
    parsed = urlsplit(expected_origin)
    if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise InputError('Service origin is not configured.')
    if (type(origin) is not str or origin != expected_origin or type(session['csrf_digest']) is not str
            or not hmac.compare_digest(credential_digest(csrf), session['csrf_digest'])):
        raise AccessError('Request origin could not be verified.')
    return owner


def submit_challenge(research, raw, *, token, session, now, origin, expected_origin, csrf):
    owner = authorize_write(token, session, now=now, origin=origin, expected_origin=expected_origin, csrf=csrf)
    payload = parse_json(raw)
    exact(payload, {'request_key', 'target_version_id', 'challenge'})
    identifier(payload['target_version_id'])
    target = research.store.connection.execute("SELECT internal_id FROM research_aliases WHERE kind='version' AND public_id=?",
                                               (payload['target_version_id'],)).fetchone()
    if target is None:
        raise InputError('Submission target is unavailable.')
    submission_id = research.register_submission(owner, payload['request_key'], payload['challenge'], target[0])
    return research.alias('submission', submission_id)


def read_submission(research, submission_id, *, token, session, now):
    owner = authenticate(token, session, now)
    identifier(submission_id)
    internal = research.store.connection.execute("SELECT internal_id FROM research_aliases WHERE kind='submission' AND public_id=?",
                                                 (submission_id,)).fetchone()
    if internal is None:
        raise InputError('Submission is unavailable.')
    submitted = research.submission(internal[0], owner)
    return {'submission_id': submission_id, 'target_version_id': research.alias('version', submitted['target_id']),
            'challenge': submitted['challenge']}
