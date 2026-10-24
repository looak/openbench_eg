# Two HTTP recorders, threads in the harness. The client recorder sits between
# client.py and the server and logs what the worker said; the nyayo recorder
# stands in for nyayo and logs every report the server's reporter sent.

import email.parser
import email.policy
import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


class Recorder:

    # A ThreadingHTTPServer that can be taken down and brought back on the same port

    def __init__(self, name):
        self.name   = name
        self.port   = 0
        self.httpd  = None
        self.thread = None
        self.lock   = threading.Lock()

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % (self.port)

    def handle(self, handler, method):
        raise NotImplementedError

    def start(self):
        recorder = self

        # HTTP/1.0, a connection per request: taking a recorder down must leave no
        # keep-alive connection still being served
        class Handler(BaseHTTPRequestHandler):

            def do_GET(self):  recorder.handle(self, 'GET')
            def do_POST(self): recorder.handle(self, 'POST')
            def do_PUT(self):  recorder.handle(self, 'PUT')

            def log_message(self, *args):
                pass

        self.httpd  = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self.port   = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name=self.name, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    @staticmethod
    def read_body(handler):
        length = int(handler.headers.get('Content-Length') or 0)
        return handler.rfile.read(length) if length else b''

    @staticmethod
    def respond(handler, status, body, content_type='application/json', headers=None):
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


class ClientRecorder(Recorder):

    # Forwards every request to the server. Requests for the paths in `watched` are
    # handed to on_event(path, form, forward), which must call forward() exactly once;
    # the harness uses it to snapshot the Test row before and after.

    def __init__(self, target, on_event, watched):
        super().__init__('smoke-client-recorder')
        self.target   = target
        self.on_event = on_event
        self.watched  = watched
        self.log      = []

    def handle(self, handler, method):
        body    = self.read_body(handler)
        path    = handler.path
        ctype   = handler.headers.get('Content-Type', '')
        entry   = { 't' : time.time(), 'method' : method, 'path' : path, 'form' : {} }

        if ctype.startswith('application/x-www-form-urlencoded'):
            entry['form'] = { k : v[-1] for k, v in urllib.parse.parse_qs(body.decode()).items() }
        elif ctype.startswith('multipart/form-data'):
            entry['form'], entry['files'] = parse_multipart(ctype, body)

        def forward():
            response = requests.request(
                method, self.target + path, data=body, allow_redirects=False, timeout=60,
                headers={ 'Content-Type' : ctype } if ctype else {})
            entry['status'] = response.status_code
            entry['reply']  = response.text[:500]
            return response

        endpoint = path.strip('/').split('/')[0].split('?')[0]
        try:
            if endpoint in self.watched:
                response = self.on_event(endpoint, entry, forward)
            else:
                response = forward()
        except requests.RequestException as error:
            entry['status'] = 502
            entry['reply']  = str(error)
            with self.lock: self.log.append(entry)
            return self.respond(handler, 502, str(error).encode(), 'text/plain')

        with self.lock: self.log.append(entry)
        self.respond(handler, response.status_code, response.content,
                     response.headers.get('Content-Type', 'application/octet-stream'))


def parse_multipart(ctype, body):
    # (fields, files) of a multipart/form-data body, the way clientSubmitPGN sends its file
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b'Content-Type: ' + ctype.encode() + b'\r\n\r\n' + body)
    fields, files = {}, {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if part.get_filename() is not None:
            files[name] = part.get_payload(decode=True)
        else:
            fields[name] = part.get_payload(decode=True).decode()
    return fields, files


class NyayoRecorder(Recorder):

    # Stands in for nyayo. Without a target it answers like nyayo, 201 for a new match or
    # game and 200 after, 404 for a game of a match it doesn't have; with one (a real nyayo)
    # it forwards and passes nyayo's answer back. Any answer but 200 or 201 is a violation
    # carrying nyayo's reason, except a game's 404, which the reporter answers by sending
    # the match first (kept in `unknown`), and a 503 asked for by busy_games. Only an
    # authorised PUT of a match or of one of its games is allowed:
    #   /api/matches/openbench-<instance>-<test id>               the match document, JSON
    #   /api/matches/openbench-<instance>-<test id>/games/<game>  one game's PGN

    def __init__(self, token, instance, target=None):
        super().__init__('smoke-nyayo-recorder')
        self.token      = token
        self.target     = target
        self.pattern    = re.compile(r'^/api/matches/(openbench-%s-(\d+))(?:/games/([A-Za-z0-9][A-Za-z0-9._-]{0,63}))?$'
                                     % (re.escape(instance)))
        self.reports    = []
        self.games      = []
        self.unknown    = []  # Games answered 404, their match not known yet: the reporter sends it, then the game
        self.violations = []
        self.seen       = set()
        self.holds      = {}  # test id -> (event, seconds): its games wait for the event, at most that long
        self.busy       = {}  # test id -> how many times each of its games is answered 503 before it's taken
        self.busied     = []  # The 503s sent

    def hold_games(self, test_id, until, longest):
        # A slow game path for one test: each of its games waits for `until`, at most `longest` seconds
        # (under the reporter's 5 s timeout, so none is lost)
        with self.lock:
            self.holds[test_id] = (until, longest)

    def busy_games(self, test_id, times):
        # nyayo busy with games: each game of the test is answered 503, Retry-After 1, `times` times first
        with self.lock:
            self.busy[test_id] = times

    def handle(self, handler, method):
        body  = self.read_body(handler)
        path  = handler.path
        match = self.pattern.match(path)
        auth  = handler.headers.get('Authorization', '')
        game  = match and match.group(3)

        problem, document = None, None
        if method != 'PUT':
            problem = '%s instead of PUT' % (method)
        elif not match:
            problem = 'unknown path'
        elif auth != 'Bearer %s' % (self.token):
            problem = 'bad Authorization header %r' % (auth)
        elif game:
            if not handler.headers.get('Content-Type', '').startswith('application/x-chess-pgn'):
                problem = 'a game sent as %r' % (handler.headers.get('Content-Type'))
        else:
            try:
                document = json.loads(body.decode() or 'null')
            except ValueError:
                problem = 'body is not JSON'
            if not problem and not isinstance(document, dict):
                problem = 'body is not a JSON object'
            elif not problem and (document.get('id') or (document.get('match') or {}).get('id')) != match.group(1):
                problem = 'document id does not match the path'

        if problem:
            with self.lock:
                self.violations.append({ 't' : time.time(), 'method' : method, 'path' : path, 'problem' : problem })
            return self.respond(handler, 400, json.dumps({ 'error' : problem }).encode())

        key, received = (match.group(1), game), time.time()
        if game:
            with self.lock:
                busy = sum(1 for x in self.busied if x['path'] == path) < self.busy.get(int(match.group(2)), 0)
                if busy:
                    self.busied.append({ 't' : received, 'test_id' : int(match.group(2)), 'game' : game, 'path' : path })
            if busy:
                return self.respond(handler, 503, b'busy, send it again', 'text/plain; charset=utf-8', { 'Retry-After' : '1' })
        hold = game and self.holds.get(int(match.group(2)))
        if hold:
            hold[0].wait(hold[1])

        if self.target:
            try:
                upstream = requests.put(self.target + path, data=body, timeout=10, headers={
                    name : handler.headers[name] for name in ('Authorization', 'Content-Type', 'Content-Encoding')
                    if handler.headers.get(name) })
                status, reply = upstream.status_code, upstream.content
            except requests.RequestException as error:
                status, reply = 502, str(error).encode()
        else:
            # As nyayo: a game of a match it doesn't have is a 404
            with self.lock:
                if game and (match.group(1), None) not in self.seen:
                    status = 404
                else:
                    status = 200 if key in self.seen else 201
                    self.seen.add(key)
            reply = json.dumps({ 'id' : match.group(1) }).encode()

        with self.lock:
            entry = { 't' : time.time(), 'received' : received, 'test_id' : int(match.group(2)), 'status' : status }
            if game and status == 404:
                self.unknown.append({ **entry, 'game' : game })
            elif game:
                self.games.append({ **entry, 'game' : game, 'pgn' : body.decode(errors='replace') })
            else:
                self.reports.append({ **entry, 'document' : document })
            if status not in (200, 201, 404) or (status == 404 and not game):
                self.violations.append({ 't' : entry['t'], 'method' : method, 'path' : path,
                                         'problem' : 'nyayo answered %d: %s' % (status, reply.decode(errors='replace')[:300]) })

        self.respond(handler, status, reply, 'text/plain; charset=utf-8')

    def for_test(self, test_id):
        with self.lock:
            return [x for x in self.reports if x['test_id'] == test_id]

    def games_for_test(self, test_id):
        with self.lock:
            return [x for x in self.games if x['test_id'] == test_id]

    def unknown_for_test(self, test_id):
        with self.lock:
            return [x for x in self.unknown if x['test_id'] == test_id]
