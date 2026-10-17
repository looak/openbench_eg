# The full smoke test: an isolated server, the real client in a temp copy of Client/,
# a test engine built with make, games played with cutechess-ob, results reported back,
# and every report the server sends to nyayo checked against Scripts/smoke/expect.py.
# Linux and WSL only. Run through Scripts/smoke_client_server.py --full.

import collections
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import requests

from smoke import expect, files
from smoke.recorders import ClientRecorder, NyayoRecorder

ROOT   = Path(__file__).resolve().parents[2]
MANAGE = ROOT / 'manage.py'

ENGINE_NAME  = 'smoke'
ENGINE_NPS   = 1000000
ENGINE_BENCH = 1000000

NYAYO_TOKEN    = 'smoke-token'
NYAYO_INSTANCE = 'smoke'

# The worker's timers, shortened in the temp copy so a scenario takes seconds, not minutes
CLIENT_TIMERS = { 'TIMEOUT_ERROR' : 1, 'TIMEOUT_WORKLOAD' : 1, 'REPORT_INTERVAL' : 2 }

# Client requests that are events in expect.py
WATCHED = { 'clientSubmitResults' : 'results', 'clientBenchError' : 'bench_error', 'clientSubmitPGN' : 'pgn' }


class SmokeFailure(Exception):
    pass


def preflight():
    missing = []
    if platform.system() != 'Linux':
        missing.append('Linux (run it in WSL)')
    for tool in ('make', 'g++'):
        if shutil.which(tool) is None:
            missing.append(tool)
    try:
        import chess  # noqa: F401
    except ImportError:
        missing.append('python-chess (pip install -r Scripts/smoke/requirements.txt)')
    if missing:
        raise SmokeFailure('--full needs: %s' % (', '.join(missing)))


class LogTail:

    # Copies a process's output to a file, keeping the last lines for failure reports

    def __init__(self, stream, path, keep=400):
        self.lines  = collections.deque(maxlen=keep)
        self.path   = path
        self.thread = threading.Thread(target=self.pump, args=(stream,), daemon=True)
        self.thread.start()

    def pump(self, stream):
        with open(self.path, 'w') as fout:
            for line in iter(stream.readline, ''):
                fout.write(line); fout.flush()
                self.lines.append(line.rstrip())

    def tail(self, count=60):
        return '\n'.join(list(self.lines)[-count:])


def stop_process(proc, group=False):
    if proc is None or proc.poll() is not None:
        return
    try:
        if group: os.killpg(proc.pid, signal.SIGTERM)
        else:     proc.terminate()
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        if group: os.killpg(proc.pid, signal.SIGKILL)
        else:     proc.kill()
        proc.wait(timeout=10)


def find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class NyayoServer:

    # A real nyayo from a checkout (--nyayo PATH), its data in a temp directory, and a
    # machine token for the reporter. The nyayo recorder forwards to it.

    def __init__(self, checkout, python, temp):
        self.checkout = Path(checkout).resolve()
        self.python   = python
        self.data     = temp / 'nyayo'
        self.port     = find_free_port()
        self.url      = 'http://127.0.0.1:%d' % (self.port)
        self.proc     = self.log = None

        if not (self.checkout / 'manage.py').is_file() or not (self.checkout / 'nyayo').is_dir():
            raise SmokeFailure('%s is not a nyayo checkout' % (self.checkout))

        # Set explicitly, so nyayo's own .env (python-dotenv never overrides) can't point it at real data
        self.env = { k : v for k, v in os.environ.items() if not k.startswith('NYAYO_') }
        self.env.update({
            'NYAYO_DATA_DIR'      : str(self.data),
            'NYAYO_DEBUG'         : '1',
            'NYAYO_SECRET_KEY'    : 'openbench-smoke',
            'NYAYO_ALLOWED_HOSTS' : '127.0.0.1,localhost',
        })

    def manage(self, *args):
        result = subprocess.run([self.python, 'manage.py'] + list(args), cwd=self.checkout, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            raise SmokeFailure('nyayo manage.py %s failed\n%s' % (args[0], result.stdout))
        return result.stdout

    def start(self):
        self.data.mkdir(parents=True)
        self.manage('migrate', '--noinput')
        self.manage('shell', '-c', (
            'from nyayo.runs.models import Machine; '
            'Machine.objects.create(name="openbench-smoke", token_hash=Machine.hash_token(%r))') % (NYAYO_TOKEN))

        self.proc = subprocess.Popen(
            [self.python, '-u', 'manage.py', 'runserver', '127.0.0.1:%d' % (self.port), '--noreload'],
            cwd=self.checkout, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.log = LogTail(self.proc.stdout, self.data.parent / 'nyayo.log')

        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SmokeFailure('nyayo exited\n%s' % (self.log.tail()))
            try:
                if requests.get(self.url + '/matches', timeout=2).status_code == 200:
                    return self
            except requests.RequestException:
                pass
            time.sleep(0.25)
        raise SmokeFailure('nyayo did not start\n%s' % (self.log.tail()))

    def query(self, sql, *params):
        db = sqlite3.connect('file:%s?mode=ro' % (self.data / 'nyayo.sqlite3'), uri=True, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]
        finally:
            db.close()

    def check_match(self, match_id, state, final):
        # nyayo's own record of the match agrees with OpenBench's Test row
        matches = self.query('SELECT state FROM matches_match WHERE id = ?', match_id)
        if not matches:
            return ['nyayo has no match %s' % (match_id)]
        problems = []
        if matches[0]['state'] != state:
            problems.append('nyayo has the match %s, expected %s' % (matches[0]['state'], state))
        rows = self.query('SELECT games, wins, draws, losses FROM matches_matchresult WHERE match_id = ?', match_id)
        want = { name : final[name] for name in ('games', 'wins', 'draws', 'losses') }
        if len(rows) != 1 or rows[0] != want:
            problems.append('nyayo has results %s, the Test has %s' % (rows, want))
        return problems

    def check_games(self, match_id, count):
        # nyayo indexed every game it took, with the subject's colour and the opponent known
        rows = self.query('SELECT game, colour, opponent, result FROM matches_game WHERE match_id = ?', match_id)
        problems = []
        if len(rows) != count:
            problems.append('nyayo has %d game(s) for the match, %d were sent' % (len(rows), count))
        unknown = [x['game'] for x in rows if x['colour'] not in ('white', 'black') or not x['opponent']]
        if unknown:
            problems.append('nyayo could not place the players of %d game(s): %s' % (len(unknown), unknown[:5]))
        return problems

    def stop(self):
        stop_process(self.proc)


class Harness:

    # One server, one client, the recorders between them. With nyayo=False the server
    # gets no NYAYO_* settings, and the nyayo recorder only listens.

    def __init__(self, args, file_server, name='main', nyayo=True, nyayo_server=None):
        self.args   = args
        self.files  = file_server
        self.name   = name
        self.temp   = Path(tempfile.mkdtemp(prefix='openbench-smoke-%s-' % (name)))
        self.port   = (args.port if name == 'main' else 0) or find_free_port()
        self.url    = 'http://127.0.0.1:%d' % (self.port)
        self.nonce  = '%s %d' % (name, time.time() * 1000)
        self.server = self.client = self.session = None
        self.server_log = self.client_log = None

        self.lock       = threading.Lock()  # Orders events: recorded client requests and actions by hand
        self.expect     = expect.Expectations(NYAYO_INSTANCE)
        self.nyayo_down = False
        self.nyayo_server = nyayo_server
        self.nyayo      = NyayoRecorder(NYAYO_TOKEN, NYAYO_INSTANCE, nyayo_server and nyayo_server.url).start()
        self.recorder   = ClientRecorder(self.url, self.on_client_event, WATCHED).start()

        self.env = os.environ.copy()
        self.env['DJANGO_SETTINGS_MODULE'] = 'OpenSite.settings_isolated'
        self.env['OPENBENCH_ISOLATED_ROOT'] = str(self.temp / 'server')
        self.env['OPENBENCH_CONFIG_DIR']    = str(self.temp / 'config')
        for key in ('NYAYO_URL', 'NYAYO_TOKEN', 'NYAYO_INSTANCE'):
            self.env.pop(key, None)
        if nyayo:
            self.env['NYAYO_URL']      = self.nyayo.url
            self.env['NYAYO_TOKEN']    = NYAYO_TOKEN
            self.env['NYAYO_INSTANCE'] = NYAYO_INSTANCE

        (self.temp / 'server').mkdir()
        self.write_config_dir()

    # Server side

    def write_config_dir(self):
        config = self.temp / 'config'
        for folder in ('Config', 'Books', 'Engines'):
            (config / folder).mkdir(parents=True)

        general = json.loads((ROOT / 'Config' / 'config.json').read_text())
        general['books']   = [files.BOOK_NAME]
        general['engines'] = [ENGINE_NAME]
        (config / 'Config' / 'config.json').write_text(json.dumps(general, indent=4))

        book = { 'sha' : files.book_sha(), 'source' : self.files.add('/books/smoke.zip', files.book_zip()) }
        (config / 'Books' / ('%s.json' % files.BOOK_NAME)).write_text(json.dumps(book, indent=4))

        engine = {
            'private' : False,
            'nps'     : ENGINE_NPS,
            'source'  : self.files.url + '/engines',
            'build'   : { 'path' : '.', 'compilers' : ['g++'], 'cpuflags' : [], 'systems' : ['Linux'] },
            'test_presets' : { 'default' : {} },
        }
        (config / 'Engines' / ('%s.json' % ENGINE_NAME)).write_text(json.dumps(engine, indent=4))

    def manage(self, *args, capture=False):
        result = subprocess.run(
            [sys.executable, str(MANAGE)] + list(args), cwd=ROOT, env=self.env,
            stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None,
            text=True, check=False)
        if result.returncode:
            raise SmokeFailure('manage.py %s failed\n%s' % (args[0], result.stdout or ''))
        return result.stdout

    def start_server(self):
        self.manage('migrate', '--noinput', capture=True)
        self.manage('shell', '-c', (
            'from django.contrib.auth.models import User; '
            'from OpenBench.models import Profile; '
            'u, _ = User.objects.get_or_create(username=%r); u.set_password(%r); u.save(); '
            'p, _ = Profile.objects.get_or_create(user=u); p.enabled = True; p.approver = True; p.save()'
        ) % (self.args.username, self.args.password), capture=True)

        self.server = subprocess.Popen(
            [sys.executable, '-u', str(MANAGE), 'runserver', '127.0.0.1:%d' % (self.port), '--noreload'],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.server_log = LogTail(self.server.stdout, self.temp / 'server.log')

        payload = { 'username' : self.args.username, 'password' : self.args.password }
        self.wait_for(lambda: self.reachable('/clientVersionRef/', payload), 30, 'the server to start')
        self.login()

    def reachable(self, path, payload):
        if self.server.poll() is not None:
            raise SmokeFailure('the server exited')
        try: return requests.post(self.url + path, data=payload, timeout=2).status_code == 200
        except requests.RequestException: return False

    def login(self):
        self.session = requests.Session()
        self.session.get(self.url + '/login/', timeout=10)
        response = self.session.post(self.url + '/login/', timeout=10, allow_redirects=False, data={
            'username' : self.args.username, 'password' : self.args.password,
            'csrfmiddlewaretoken' : self.session.cookies.get('csrftoken', ''),
        })
        if 'sessionid' not in self.session.cookies:
            raise SmokeFailure('logging in failed: %d %s' % (response.status_code, response.text[:300]))

    def engine(self, label, bench=ENGINE_BENCH):
        # Each engine gets its own sha, so the worker builds it from its own zip
        sha    = hashlib.sha1(('%s %s %s' % (self.nonce, label, time.time())).encode()).hexdigest()
        source = self.files.add('/engines/%s.zip' % (sha), files.engine_zip('smoke-%s' % (sha)))
        return { 'name' : label, 'source' : source, 'sha' : sha, 'bench' : bench }

    def create_test(self, dev=None, base=None, **overrides):
        dev  = dev  or self.engine('dev')
        base = base or self.engine('base')
        test = {
            'author'            : self.args.username,
            'book_name'         : files.BOOK_NAME,
            'dev_repo'          : self.files.url + '/engines',
            'dev_engine'        : ENGINE_NAME,
            'dev_options'       : 'Threads=1 Hash=16',
            'dev_time_control'  : 'N=1000',
            'base_repo'         : self.files.url + '/engines',
            'base_engine'       : ENGINE_NAME,
            'base_options'      : 'Threads=1 Hash=16',
            'base_time_control' : 'N=1000',
            'workload_size'     : 2,
            'priority'          : 1,
            'throughput'        : 100,
            'scale_nps'         : ENGINE_NPS,
            'test_mode'         : 'GAMES',
            'max_games'         : 8,
            'approved'          : True,
            'awaiting'          : False,
        }
        test.update(overrides)

        if test['test_mode'] == 'SPRT':
            test['lowerllr'] = math.log(test['beta'] / (1.0 - test['alpha']))
            test['upperllr'] = math.log((1.0 - test['beta']) / test['alpha'])

        output = self.manage('shell', '-c', (
            'import json; from OpenBench.models import Engine, Test; '
            'f = json.loads(%r); '
            'dev = Engine.objects.create(**f.pop("dev")); base = Engine.objects.create(**f.pop("base")); '
            'print("SMOKE_TEST_ID", Test.objects.create(dev=dev, base=base, **f).id)'
        ) % (json.dumps({ **test, 'dev' : dev, 'base' : base })), capture=True)

        match = re.search(r'SMOKE_TEST_ID (\d+)', output)
        if not match:
            raise SmokeFailure('creating a test printed no id\n%s' % (output))
        return int(match.group(1))

    def query(self, sql, *params):
        db = sqlite3.connect('file:%s?mode=ro' % (self.temp / 'server' / 'db.sqlite3'), uri=True, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]
        finally:
            db.close()

    def test_row(self, test_id):
        return self.query('SELECT * FROM OpenBench_test WHERE id = ?', test_id)[0]

    def results(self, test_id):
        return self.query('SELECT * FROM OpenBench_result WHERE test_id = ?', test_id)

    # Events: what the worker said, and what the harness did by hand

    def on_client_event(self, endpoint, entry, forward):
        test_id = int(entry['form']['test_id'])
        with self.lock:
            before   = self.test_row(test_id)
            response = forward()
            after    = self.test_row(test_id)
            if response.status_code == 200:
                event = self.expect.record(entry['t'], WATCHED[endpoint], test_id, before, after, lost=self.nyayo_down)
                if event.kind == 'pgn':
                    event.upload     = entry.get('files', {}).get('file', b'')
                    event.result_id  = entry['form']['result_id']
                    event.book_index = entry['form']['book_index']
        return response

    def action(self, test_id, action, **fields):
        # A workload action by hand, as the test page's buttons do it
        self.settle(test_id)
        with self.lock:
            before   = self.test_row(test_id)
            response = self.session.post(
                '%s/test/%d/%s/' % (self.url, test_id, action), data=fields, allow_redirects=False, timeout=10,
                headers={ 'X-CSRFToken' : self.session.cookies.get('csrftoken', '') })
            after    = self.test_row(test_id)
            if response.status_code != 302 or 'error' in response.headers.get('Location', ''):
                raise SmokeFailure('%s on test %d: %d %s' % (action, test_id, response.status_code,
                                                             response.headers.get('Location', response.text[:200])))
            self.expect.record(time.time(), action, test_id, before, after, lost=self.nyayo_down)
        return after

    def settle(self, test_id, timeout=15):
        # Reports go through a queue on a background thread: wait until every expected one is in
        want = lambda: len(self.expect.expected(test_id))
        self.wait_for(lambda: len(self.nyayo.for_test(test_id)) >= want(), timeout,
                      '%d report(s) for test %d' % (want(), test_id), soft=True)
        time.sleep(0.3)

    def events(self, test_id, kind=None):
        return [x for x in self.expect.events if x.test_id == test_id and (kind is None or x.kind == kind)]

    def wait_until_released(self, test_id, timeout=30):
        # The worker learns a test ended at its next submission, which then changes nothing,
        # or it had finished the workload already and asks for the next one
        since = time.time()
        count = len(self.events(test_id))
        asked = lambda: any(x['path'].startswith('/clientGetWorkload') and x['t'] > since for x in list(self.recorder.log))
        self.wait_for(lambda: any(not x.report for x in self.events(test_id)[count:]) or asked(),
                      timeout, 'the worker to leave test %d' % (test_id))

    def verify(self, test_id):
        self.settle(test_id)
        problems = self.expect.check(
            test_id, self.nyayo.for_test(test_id), self.test_row(test_id), self.results(test_id))
        problems += expect.check_games(
            self.events(test_id), self.nyayo.games_for_test(test_id), self.nyayo.for_test(test_id))[0]
        if self.nyayo_server:
            problems += self.nyayo_server.check_games(
                'openbench-%s-%d' % (NYAYO_INSTANCE, test_id), len(self.nyayo.games_for_test(test_id)))
        problems += ['nyayo recorder: %s %s: %s' % (x['method'], x['path'], x['problem']) for x in self.nyayo.violations]
        if self.nyayo_server and self.expect.expected(test_id):
            problems += self.nyayo_server.check_match('openbench-%s-%d' % (NYAYO_INSTANCE, test_id),
                                                      self.expect.state[test_id], self.test_row(test_id))
        if problems:
            raise SmokeFailure('test %d:\n  - %s\n\n%s' % (test_id, '\n  - '.join(problems), expect.timeline(
                test_id, list(self.recorder.log), self.expect.events, self.nyayo.for_test(test_id))))

    # Client side

    def start_client(self):
        client = self.temp / 'Client'
        shutil.copytree(ROOT / 'Client', client, ignore=shutil.ignore_patterns(
            '__pycache__', 'PGNs', 'Engines', 'Networks', 'Books', '*.exe', 'openbench.exit'))

        worker = (client / 'worker.py').read_text()
        for name, value in CLIENT_TIMERS.items():
            worker, count = re.subn(r'^(%s\s*=\s*)\d+' % (name), r'\g<1>%d' % (value), worker, flags=re.M)
            if count != 1:
                raise SmokeFailure('worker.py has no %s to shorten' % (name))
        (client / 'worker.py').write_text(worker)

        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['PYTHON'] = sys.executable  # The test engine's makefile writes this into the shebang
        env['PATH']   = os.path.dirname(sys.executable) + os.pathsep + env.get('PATH', '')

        self.client = subprocess.Popen(
            [sys.executable, '-u', str(client / 'client.py'),
             '-U', self.args.username, '-P', self.args.password, '-S', self.recorder.url,
             '-T', '1', '-N', '1', '-I', 'smoke-%s' % (self.name), '--no-client-downloads'],
            cwd=client, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        self.client_log = LogTail(self.client.stdout, self.temp / 'client.log')

    def client_engines(self):
        folder = self.temp / 'Client' / 'Engines'
        return sorted(x.name for x in folder.iterdir()) if folder.is_dir() else []

    # Shared

    def wait_for(self, predicate, timeout, what, soft=False):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.client is not None and self.client.poll() is not None:
                raise SmokeFailure('the client exited while waiting for %s' % (what))
            if predicate():
                return True
            time.sleep(0.25)
        if soft:
            return False
        raise SmokeFailure('timed out after %ds waiting for %s' % (timeout, what))

    def report_logs(self):
        for name, log in (('server', self.server_log), ('client', self.client_log)):
            if log:
                print('\n---- %s %s log (tail; full log in %s) ----\n%s' % (self.name, name, log.path, log.tail()))

    def close(self, keep):
        stop_process(self.client, group=True)
        stop_process(self.server)
        self.recorder.stop()
        self.nyayo.stop()
        if keep: print('Kept %s' % (self.temp))
        else:    shutil.rmtree(self.temp, ignore_errors=True)


def check(condition, message):
    if not condition:
        raise SmokeFailure(message)


def score(row):
    return '%d games (+%d =%d -%d)' % (row['games'], row['wins'], row['draws'], row['losses'])


# Scenarios: each a fresh test on the same server and client, leaving it inactive

def scenario_games_to_max(h):
    test_id = h.create_test(test_mode='GAMES', max_games=8)
    h.wait_for(lambda: h.test_row(test_id)['finished'], 90, 'test %d to finish' % (test_id))

    test, results = h.test_row(test_id), h.results(test_id)
    check(test['games'] == test['max_games'], 'played %d games, max_games is %d' % (test['games'], test['max_games']))
    check(test['wins'] + test['draws'] + test['losses'] == test['games'], 'W+D+L is not the game count')
    check(test['LL'] + test['LD'] + test['DD'] + test['DW'] + test['WW'] == test['games'] // 2,
          'the pentanomial counts are not half the game count')
    check(len(results) == 1 and results[0]['games'] == test['games'], 'the Result row disagrees with the Test')
    check(len(h.client_engines()) >= 2, 'the client did not build both engines: %s' % (h.client_engines()))
    check(len(h.events(test_id, 'results')) >= 2, 'expected a report per batch, saw one batch')

    h.verify(test_id)
    return 'test %d: %s, %d reports' % (test_id, score(test), len(h.nyayo.for_test(test_id)))

def scenario_sprt_to_result(h):
    # The base plays any legal move, so the dev wins nearly every pair and the SPRT ends quickly
    test_id = h.create_test(test_mode='SPRT', elolower=0.0, eloupper=200.0, alpha=0.05, beta=0.05,
                            base_options='Threads=1 Hash=16 Strength=0')
    h.wait_for(lambda: h.test_row(test_id)['finished'], 120, 'SPRT test %d to finish' % (test_id))

    test = h.test_row(test_id)
    check(test['passed'] or test['failed'], 'the SPRT finished with neither a pass nor a fail')
    h.verify(test_id)
    return 'test %d: %s, LLR %.2f, %s' % (test_id, score(test), test['currentllr'], 'passed' if test['passed'] else 'failed')

def scenario_stop(h):
    # Approved and modified by hand first: each a report with the state unchanged
    test_id = h.create_test(test_mode='GAMES', max_games=1000, approved=False)
    h.action(test_id, 'APPROVE')
    h.action(test_id, 'MODIFY', priority=1, throughput=50, workload_size=2)
    h.wait_for(lambda: h.test_row(test_id)['games'] >= 4, 60, 'test %d to play 4 games' % (test_id))

    h.action(test_id, 'STOP')
    h.wait_until_released(test_id)
    h.verify(test_id)
    return 'test %d: stopped at %s' % (test_id, score(h.test_row(test_id)))

def scenario_delete(h):
    test_id = h.create_test(test_mode='GAMES', max_games=1000)
    h.wait_for(lambda: h.test_row(test_id)['games'] >= 4, 60, 'test %d to play 4 games' % (test_id))

    h.action(test_id, 'DELETE')
    h.wait_until_released(test_id)
    h.verify(test_id)
    return 'test %d: deleted at %s' % (test_id, score(h.test_row(test_id)))

def scenario_restore(h):
    test_id = h.create_test(test_mode='GAMES', max_games=1000)
    h.wait_for(lambda: h.test_row(test_id)['games'] >= 4, 60, 'test %d to play 4 games' % (test_id))
    h.action(test_id, 'DELETE')
    h.wait_until_released(test_id)

    games = h.action(test_id, 'RESTORE')['games']
    h.wait_for(lambda: h.test_row(test_id)['games'] > games, 60, 'restored test %d to play again' % (test_id))
    h.action(test_id, 'STOP')
    h.wait_until_released(test_id)
    h.verify(test_id)
    return 'test %d: restored at %d games, stopped at %s' % (test_id, games, score(h.test_row(test_id)))

def scenario_bench_mismatch(h):
    test_id = h.create_test(test_mode='GAMES', max_games=8, dev=h.engine('dev', bench=ENGINE_BENCH + 1))
    h.wait_for(lambda: h.events(test_id, 'bench_error'), 60, 'a bench error for test %d' % (test_id))
    h.wait_for(lambda: h.test_row(test_id)['finished'], 10, 'test %d to be stopped' % (test_id))

    check(h.test_row(test_id)['games'] == 0, 'games were played after a bench mismatch')
    h.verify(test_id)
    return 'test %d: bench error, stopped' % (test_id)

def scenario_crash_then_stop(h):
    # The dev exits after a few moves in every game; tight error bounds keep the SPRT open
    test_id = h.create_test(test_mode='SPRT', elolower=0.0, eloupper=5.0, alpha=1e-6, beta=1e-6,
                            dev_options='Threads=1 Hash=16 CrashAfter=6')
    h.wait_for(lambda: sum(x['crashes'] for x in h.results(test_id)) > 0, 60, 'test %d to count a crash' % (test_id))

    check(not h.test_row(test_id)['finished'], 'the SPRT finished before it could be stopped')
    h.action(test_id, 'STOP')
    h.wait_until_released(test_id)
    h.verify(test_id)
    return 'test %d: %d crashes, stopped' % (test_id, sum(x['crashes'] for x in h.results(test_id)))

def scenario_nyayo_down(h):
    test_id = h.create_test(test_mode='GAMES', max_games=16)
    h.wait_for(lambda: h.nyayo.for_test(test_id), 60, 'the first report for test %d' % (test_id))

    with h.lock:
        h.nyayo_down = True
        h.nyayo.stop()
    h.wait_for(lambda: any(not x.report for x in h.events(test_id)), 60, 'a batch while nyayo is down')
    lost_at = len(h.recorder.log)
    time.sleep(1)  # Let the reporter fail on it

    with h.lock:
        h.nyayo.start()
        h.nyayo_down = False

    h.wait_for(lambda: h.test_row(test_id)['finished'], 90, 'test %d to finish' % (test_id))
    h.verify(test_id)

    # The worker never noticed: every request it made was answered by the server
    failed = [x for x in list(h.recorder.log)[:lost_at] if x.get('status') != 200]
    check(not failed, 'the worker saw failures while nyayo was down: %s' % (failed))
    check('failed to report match for test %d' % (test_id) in '\n'.join(h.server_log.lines),
          'the server never logged the failed report')
    return 'test %d: %d batch(es) lost, then %s' % (
        test_id, sum(1 for x in h.events(test_id) if not x.report and x.lost), score(h.test_row(test_id)))

def scenario_no_nyayo(h):
    # A second server and client with no NYAYO_* settings; the recorder only listens
    other = Harness(h.args, h.files, name='upstream', nyayo=False)
    try:
        other.start_server()
        other.start_client()
        test_id = other.create_test(test_mode='GAMES', max_games=8)
        other.wait_for(lambda: other.test_row(test_id)['finished'], 90, 'test %d to finish' % (test_id))
        time.sleep(1)
        heard = len(other.nyayo.reports) + len(other.nyayo.violations)
        check(heard == 0, 'the recorder got %d request(s) from a server with no NYAYO_URL' % (heard))
        check(other.test_row(test_id)['games'] == 8, 'the test did not play its 8 games')
        return 'test %d on a second server: %s, no reports' % (test_id, score(other.test_row(test_id)))
    except Exception:
        other.report_logs()
        raise
    finally:
        other.close(keep=h.args.keep)


def scenario_pgns(h):
    test_id = h.create_test(test_mode='GAMES', max_games=8, upload_pgns='COMPACT')
    h.wait_for(lambda: h.test_row(test_id)['finished'], 90, 'test %d to finish' % (test_id))
    h.wait_for(lambda: len(h.events(test_id, 'pgn')) * 4 >= h.test_row(test_id)['games'], 30,
               'the worker to upload the PGNs of test %d' % (test_id))
    _, want = expect.check_games(h.events(test_id), [], [])
    h.wait_for(lambda: len(h.nyayo.games_for_test(test_id)) >= want, 15, '%d game(s) in nyayo' % (want), soft=True)

    check(want == h.test_row(test_id)['games'], 'uploaded %d games, the test played %d' % (want, h.test_row(test_id)['games']))
    h.verify(test_id)
    return 'test %d: %d batches, %d games in nyayo' % (
        test_id, len(h.events(test_id, 'pgn')), len(h.nyayo.games_for_test(test_id)))


SCENARIOS = collections.OrderedDict([
    ('1',  ('GAMES test to max_games',          scenario_games_to_max)),
    ('2',  ('SPRT with wide bounds to a result', scenario_sprt_to_result)),
    ('3',  ('stop mid-test',                     scenario_stop)),
    ('4',  ('delete mid-test, not stopped',      scenario_delete)),
    ('5',  ('restore after a delete',            scenario_restore)),
    ('6',  ('bench mismatch',                    scenario_bench_mismatch)),
    ('7',  ('engine crashes, then stopped',      scenario_crash_then_stop)),
    ('8',  ('nyayo down for a while',            scenario_nyayo_down)),
    ('9',  ('no NYAYO_URL',                      scenario_no_nyayo)),
    ('10', ('upload_pgns=COMPACT reaches nyayo', scenario_pgns)),
])

# Not written yet, each waiting on the work named
PENDING = {
    '11' : ('report_to_nyayo TEST_ID',           'the backfill command, openbench-reports.md'),
}


def run_scenario(harness, key):
    title, run = SCENARIOS[key]
    xfail, began = expect.XFAIL.get(key), time.time()
    try:
        detail = run(harness)
    except SmokeFailure as error:
        took = time.time() - began
        if xfail:
            print('XFAIL %-3s %s (%.1fs): %s\n      %s' % (key, title, took, xfail, str(error).split('\n')[0]))
            if harness.args.verbose: print(error)
            return True
        print('FAIL  %-3s %s (%.1fs): %s' % (key, title, took, error))
        return False
    except Exception:
        print('FAIL  %-3s %s (%.1fs)' % (key, title, time.time() - began))
        traceback.print_exc()
        return False

    if xfail:
        print('XPASS %-3s %s (%.1fs): passes now, remove its mark in expect.py (%s)' % (key, title, time.time() - began, xfail))
        return False
    print('PASS  %-3s %s (%.1fs) %s' % (key, title, time.time() - began, detail or ''))
    return True


def main(args):
    preflight()

    selected = args.only.split(',') if args.only else list(SCENARIOS) + list(PENDING)
    unknown  = [x for x in selected if x not in SCENARIOS and x not in PENDING]
    if unknown:
        raise SmokeFailure('unknown scenario(s): %s' % (', '.join(unknown)))

    started, failed, nyayo = time.time(), [], None
    with files.FileServer() as file_server:
        harness = Harness(args, file_server)
        try:
            if args.nyayo:
                nyayo = NyayoServer(args.nyayo, args.nyayo_python or sys.executable, harness.temp).start()
                harness.nyayo.target, harness.nyayo_server = nyayo.url, nyayo
            harness.start_server()
            harness.start_client()

            for key in selected:
                if key in PENDING:
                    print('SKIP  %-3s %s (waits on %s)' % (key, *PENDING[key]))
                elif not run_scenario(harness, key):
                    failed.append(key)

            if failed:
                harness.report_logs()

        except Exception:
            harness.report_logs()
            raise

        finally:
            if nyayo:
                if failed: print('\n---- nyayo log (tail; full log in %s) ----\n%s' % (nyayo.log.path, nyayo.log.tail()))
                nyayo.stop()
            harness.close(keep=args.keep or bool(failed))

    print('\nSMOKE FULL %s in %.1fs' % ('FAIL (%s)' % (', '.join(failed)) if failed else 'PASS', time.time() - started))
    return 1 if failed else 0
