from dataclasses import dataclass, field
import re

from evorph.contracts import InputError, canonical, exact, integer, parse_json
from runtime.store import ConflictError
from .access import read_submission, submit_challenge
from .public_store import RateLimited

MAX_BODY_BYTES = 65536
READ_PATH = re.compile(r'^/api/research/experiments(?:/(?P<experiment>[A-Za-z0-9._:-]{1,120})/records)?$')
CHALLENGE_PATH = re.compile(r'^/api/challenges(?:/(?P<submission>[A-Za-z0-9._:-]{1,120}))?$')


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict, repr=False)
    body: bytes = field(default=b'', repr=False)
    client_key: str = 'unspecified'


@dataclass(frozen=True)
class Response:
    status: int
    body: dict = field(repr=False)
    headers: tuple = field(default=(), repr=False)


def _text(status, reason):
    return Response(status, {'error': reason})


def cookie(headers, name):
    matches = []
    for item in (headers.get('cookie') or '').split(';'):
        key, _, value = item.strip().partition('=')
        if key == name:
            matches.append(value)
    if len(matches) > 1:
        raise InputError('Ambiguous request credentials.')
    return matches[0] if matches else None


def session_response(status, issued, lifetime):
    token = issued['credentials']['token']
    return Response(status, {'contributor_id': issued['contributor_id'], 'csrf_token': issued['credentials']['csrf'],
                             'recovery': issued['recovery']},
                    (('Set-Cookie', 'evorph_csrf=' + issued['credentials']['csrf'] + '; Path=/; Max-Age=' + str(lifetime) + '; Secure; SameSite=Strict'),
                     ('Set-Cookie', 'evorph_session=' + token + '; Path=/; Max-Age=' + str(lifetime) +
                      '; Secure; HttpOnly; SameSite=Strict')))


def query_value(query, name):
    value = query.get(name)
    if value is None:
        return None
    if type(value) is list:
        if len(value) != 1:
            raise InputError('Repeated query parameter.')
        value = value[0]
    if type(value) is not str:
        raise InputError('Invalid query parameter.')
    return value


def optional_int(query, name):
    value = query_value(query, name)
    if value is None:
        return None
    if not re.fullmatch(r'[0-9]{1,15}', value):
        raise InputError('Invalid integer parameter.')
    return int(value)


def dispatch(service, request, *, origin):
    """Read paths have no execution or spending side effects; writes require a verified session."""
    if type(request.body) is not bytes or len(request.body) > MAX_BODY_BYTES:
        return _text(413, 'request_too_large')
    try:
        return _route(service, request, origin=origin)
    except RateLimited:
        return _text(429, 'rate_limited')
    except ConflictError:
        return _text(409, 'conflict')
    except PermissionError:
        return _text(401, 'unauthorized')
    except InputError:
        return _text(400, 'invalid_request')
    except Exception:
        return _text(500, 'service_unavailable')


def _route(service, request, *, origin):
    method, path = request.method, request.path
    if method not in ('GET', 'POST'):
        return _text(405, 'method_not_allowed')
    if method == 'GET':
        if path in ('/api/research/status', '/api/identities/session', '/api/challenges/config'):
            service.charge('read:' + request.client_key, service.limits.read_per_window)
            if path == '/api/challenges/config':
                if set(request.query) != {'experiment_id'}:
                    raise InputError('Experiment selection is required.')
                return Response(200, service.challenge_config(query_value(request.query, 'experiment_id')))
            if request.query:
                raise InputError('Unexpected query field.')
            if path == '/api/research/status':
                return Response(200, {'mode': service.mode, 'connected': True})
            token = cookie(request.headers, 'evorph_session')
            try:
                session = service.session(token)
            except PermissionError:
                session = None
            return Response(200, {'authenticated': session is not None,
                                  'contributor_id': service.research.alias('contributor', session['owner_id']) if session else None})
        match = READ_PATH.match(path)
        if match:
            allowed = {'cursor', 'limit'} | ({'after_sequence'} if match['experiment'] else set())
            if not set(request.query) <= allowed:
                raise InputError('Unknown query field.')
            service.charge('read:' + request.client_key, service.limits.read_per_window)
            limit = optional_int(request.query, 'limit')
            if match['experiment'] is None:
                return Response(200, service.list_experiments(query_value(request.query, 'cursor'), limit))
            return Response(200, service.list_records(match['experiment'], query_value(request.query, 'cursor'),
                                                      optional_int(request.query, 'after_sequence'), limit))
        challenge = CHALLENGE_PATH.match(path)
        if challenge and challenge['submission'] is not None:
            service.charge('read:' + request.client_key, service.limits.read_per_window)
            token = cookie(request.headers, 'evorph_session')
            body = read_submission(service.research, challenge['submission'], token=token,
                                   session=service.session(token), now=int(service.now()))
            body['state'] = service.submission_state(challenge['submission'])
            return Response(200, body)
        return _text(404, 'unknown_path')
    if request.headers.get('origin') != origin:
        raise PermissionError('Request origin could not be verified.')
    if request.query or request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
        raise InputError('Expected a JSON request.')
    if path in ('/api/identities', '/api/identities/recovery'):
        if request.body:
            exact(parse_json(request.body), set())
        if path == '/api/identities':
            issued = service.create_identity(request.client_key)
            return session_response(201, issued, service.limits.session_seconds)
        issued = service.recover_identity(_bearer(request), request.client_key)
        return session_response(200, issued, service.limits.session_seconds)
    match = CHALLENGE_PATH.match(path)
    if not match or match['submission'] is not None:
        return _text(404 if not match else 405, 'unknown_path' if not match else 'method_not_allowed')
    service.charge('write:' + request.client_key, service.limits.write_per_window)
    token = cookie(request.headers, 'evorph_session')
    submission = submit_challenge(service.research, request.body, token=token, session=service.session(token),
                                  now=int(service.now()), origin=request.headers.get('origin'),
                                  expected_origin=origin, csrf=request.headers.get('x-csrf-token'))
    return Response(202, {'submission_id': submission, 'state': 'queued'})


def _bearer(request):
    header = request.headers.get('authorization') or ''
    scheme, _, value = header.partition(' ')
    if scheme.lower() != 'bearer' or not value:
        raise InputError('Recovery credentials are unavailable.')
    return value


def handler(service, *, origin, request_timeout):
    from http.server import BaseHTTPRequestHandler
    import socket
    from threading import Timer
    from urllib.parse import parse_qs, urlsplit

    integer(request_timeout, 1, 3600)
    configured = urlsplit(origin)
    if (configured.scheme != 'https' or not configured.netloc or configured.username or configured.password
            or configured.path or configured.query or configured.fragment):
        raise InputError('Expected a configured HTTPS origin.')

    class ServiceHandler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def setup(self):
            super().setup()
            self.connection.settimeout(request_timeout)
            self.expired = False
            self.deadline = Timer(request_timeout, self._expire)
            self.deadline.daemon = True
            self.deadline.start()

        def _expire(self):
            self.expired = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self.deadline.cancel()
            super().finish()

        def _respond(self, response):
            if self.expired:
                self.close_connection = True
                return
            raw = canonical(response.body)
            self.close_connection = True
            self.send_response(response.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for key, value in response.headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def send_error(self, code, message=None, explain=None):
            self._respond(_text(code, 'invalid_request'))

        def _serve(self, method):
            try:
                lengths = self.headers.get_all('Content-Length', [])
                sensitive = ('Cookie', 'Origin', 'Authorization', 'Content-Type', 'X-CSRF-Token')
                if (len(lengths) > 1 or self.headers.get('Transfer-Encoding') is not None
                        or any(len(self.headers.get_all(name, [])) > 1 for name in sensitive)):
                    raise InputError('Ambiguous request framing.')
                value = lengths[0] if lengths else '0'
                if not re.fullmatch(r'[0-9]{1,10}', value):
                    raise InputError('Invalid request length.')
                length = int(value)
                if length > MAX_BODY_BYTES:
                    self._respond(_text(413, 'request_too_large'))
                    return
                if method == 'GET' and length:
                    raise InputError('Unexpected request body.')
                parts = urlsplit(self.path)
                if parts.scheme or parts.netloc or not self.path.startswith('/') or self.path.startswith('//'):
                    raise InputError('Unexpected request target.')
                body = self.rfile.read(length) if length else b''
                if len(body) != length:
                    raise InputError('Incomplete request body.')
                headers = {key.lower(): val for key, val in self.headers.items()}
                query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=10)
                request = Request(method, parts.path, query, headers, body, self.client_address[0])
                self._respond(dispatch(service, request, origin=origin))
            except (InputError, ValueError):
                try:
                    self._respond(_text(400, 'invalid_request'))
                except OSError:
                    self.close_connection = True
            except (TimeoutError, OSError):
                self.close_connection = True

        def do_GET(self):
            self._serve('GET')

        def do_POST(self):
            self._serve('POST')

        def log_message(self, *_args):
            pass

    return ServiceHandler
