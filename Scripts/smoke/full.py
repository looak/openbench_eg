# The full smoke test: an isolated server, the real client in a temp copy of Client/,
# a test engine built with make, games played with cutechess-ob, results reported back.
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

from smoke import files

ROOT   = Path(__file__).resolve().parents[2]
MANAGE = ROOT / 'manage.py'

ENGINE_NAME = 'smoke'
ENGINE_NPS  = 1000000
ENGINE_BENCH = 1000000

# The worker's timers, shortened in the temp copy so a scenario takes seconds, not minutes
CLIENT_TIMERS = { 'TIMEOUT_ERROR' : 1, 'TIMEOUT_WORKLOAD' : 1, 'REPORT_INTERVAL' : 2 }


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

    def __init__(self, stream, path, keep=200):
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


class Harness:

    def __init__(self, args, file_server):
        self.args     = args
        self.files    = file_server
        self.temp     = Path(tempfile.mkdtemp(prefix='openbench-smoke-full-'))
        self.port     = args.port or find_free_port()
        self.url      = 'http://127.0.0.1:%d' % (self.port)
        self.server   = self.client = None
        self.server_log = self.client_log = None
        self.nonce    = '%d' % (time.time() * 1000)

        self.env = os.environ.copy()
        self.env['DJANGO_SETTINGS_MODULE'] = 'OpenSite.settings_isolated'
        self.env['OPENBENCH_ISOLATED_ROOT'] = str(self.temp / 'server')
        self.env['OPENBENCH_CONFIG_DIR']    = str(self.temp / 'config')
        for name in ('NYAYO_URL', 'NYAYO_TOKEN', 'NYAYO_INSTANCE'):
            self.env.pop(name, None)

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

    def reachable(self, path, payload):
        if self.server.poll() is not None:
            raise SmokeFailure('the server exited')
        try: return requests.post(self.url + path, data=payload, timeout=2).status_code == 200
        except requests.RequestException: return False

    def engine(self, label, bench=ENGINE_BENCH):
        # Each engine gets its own sha, so the worker builds it from its own zip
        sha    = hashlib.sha1(('%s %s %s' % (self.nonce, label, time.time())).encode()).hexdigest()
        source = self.files.add('/engines/%s.zip' % (sha), files.engine_zip('smoke-%s' % (sha)))
        return { 'name' : label, 'source' : source, 'sha' : sha, 'bench' : bench }

    def create_test(self, **overrides):
        dev  = overrides.pop('dev',  None) or self.engine('dev')
        base = overrides.pop('base', None) or self.engine('base')
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
             '-U', self.args.username, '-P', self.args.password, '-S', self.url,
             '-T', '1', '-N', '1', '-I', 'smoke-worker', '--no-client-downloads'],
            cwd=client, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        self.client_log = LogTail(self.client.stdout, self.temp / 'client.log')

    def client_engines(self):
        folder = self.temp / 'Client' / 'Engines'
        return sorted(x.name for x in folder.iterdir()) if folder.is_dir() else []

    # Shared

    def wait_for(self, predicate, timeout, what):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.client is not None and self.client.poll() is not None:
                raise SmokeFailure('the client exited while waiting for %s' % (what))
            if predicate():
                return
            time.sleep(0.5)
        raise SmokeFailure('timed out after %ds waiting for %s' % (timeout, what))

    def report_logs(self):
        for name, log in (('server', self.server_log), ('client', self.client_log)):
            if log:
                print('\n---- %s log (tail; full log in %s) ----\n%s' % (name, log.path, log.tail()))

    def close(self, keep):
        stop_process(self.client, group=True)
        stop_process(self.server)
        if keep: print('Kept %s' % (self.temp))
        else:    shutil.rmtree(self.temp, ignore_errors=True)


def find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


# Scenarios: each a fresh test on the same server and client

def scenario_games_to_max(h):
    test_id = h.create_test(test_mode='GAMES', max_games=8)
    h.wait_for(lambda: h.test_row(test_id)['finished'], 90, 'test %d to finish' % (test_id))

    test    = h.test_row(test_id)
    results = h.results(test_id)

    check(test['games'] == test['max_games'], 'played %d games, max_games is %d' % (test['games'], test['max_games']))
    check(test['wins'] + test['draws'] + test['losses'] == test['games'], 'W+D+L is not the game count')
    check(test['LL'] + test['LD'] + test['DD'] + test['DW'] + test['WW'] == test['games'] // 2,
          'the pentanomial counts are not half the game count')
    check(len(results) == 1 and results[0]['games'] == test['games'], 'the Result row disagrees with the Test')
    check(not test['error'], 'the test is marked as an error')
    check(len(h.client_engines()) >= 2, 'the client did not build both engines: %s' % (h.client_engines()))
    return 'test %d: %d games (+%d =%d -%d)' % (test_id, test['games'], test['wins'], test['draws'], test['losses'])


SCENARIOS = collections.OrderedDict([
    ('1', ('GAMES test to max_games', scenario_games_to_max)),
])


def check(condition, message):
    if not condition:
        raise SmokeFailure(message)


def main(args):
    preflight()

    selected = args.only.split(',') if args.only else list(SCENARIOS)
    unknown  = [x for x in selected if x not in SCENARIOS]
    if unknown:
        raise SmokeFailure('unknown scenario(s): %s' % (', '.join(unknown)))

    started, failed = time.time(), []
    with files.FileServer() as file_server:
        harness = Harness(args, file_server)
        try:
            harness.start_server()
            harness.start_client()

            for key in selected:
                title, run = SCENARIOS[key]
                began = time.time()
                try:
                    detail = run(harness)
                    print('PASS %-3s %s (%.1fs) %s' % (key, title, time.time() - began, detail or ''))
                except SmokeFailure as error:
                    failed.append(key)
                    print('FAIL %-3s %s (%.1fs): %s' % (key, title, time.time() - began, error))
                except Exception:
                    failed.append(key)
                    print('FAIL %-3s %s (%.1fs)' % (key, title, time.time() - began))
                    traceback.print_exc()

            if failed:
                harness.report_logs()

        except Exception:
            harness.report_logs()
            raise

        finally:
            harness.close(keep=args.keep or bool(failed))

    print('\nSMOKE FULL %s in %.1fs' % ('FAIL (%s)' % (', '.join(failed)) if failed else 'PASS', time.time() - started))
    return 1 if failed else 0
