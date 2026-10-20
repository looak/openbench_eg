import bz2
import collections
import logging
import queue
import re
import threading

import requests
from django.conf import settings
from django.utils import timezone

import OpenBench.stats

from OpenBench.models import LogEvent, Result, Test

logger = logging.getLogger(__name__)


class LatestQueue:

    # The tests whose match is waiting to be sent, each once and in the order first queued. A test
    # queued again while it waits keeps its place: its document is built when it's sent, so the
    # report sent is the newest anyway.

    def __init__(self):
        self.order   = collections.deque()
        self.waiting = set()
        self.ready   = threading.Condition()

    def put(self, test_id):
        with self.ready:
            if test_id not in self.waiting:
                self.waiting.add(test_id)
                self.order.append(test_id)
                self.ready.notify()

    def get(self, timeout):
        with self.ready:
            if not self.order and not self.ready.wait_for(lambda: self.order, timeout):
                raise queue.Empty
            test_id = self.order.popleft()
            self.waiting.discard(test_id)
            return test_id


# Match reports and games each have their own queue and thread, so a test's status never waits
# behind games, its own or another test's (ramani plans/live-updates.md, Status apart from games)
REPORT_QUEUE = LatestQueue()
GAME_QUEUE   = queue.Queue()
REPORT_STOP  = threading.Event()
REPORT_THREAD = GAME_THREAD = None


def nyayo_enabled():
    return bool(
        getattr(settings, 'NYAYO_URL', '').strip()
        and getattr(settings, 'NYAYO_TOKEN', '').strip()
        and getattr(settings, 'NYAYO_INSTANCE', '').strip()
    )


def build_match_id(test):
    instance = getattr(settings, 'NYAYO_INSTANCE', 'local')
    return 'openbench-%s-%s' % (instance, test.id)


def stopped_by_bench_error(test):
    # The test's newest bench error is newer than its newest action by hand (a restart, say).
    # Only client_bench_error logs a worker's event without a log file; a worker's other
    # errors (crashes, illegal moves) come with one, and may arrive after a stop by hand.
    events   = LogEvent.objects.filter(test_id=test.id)
    bench    = events.exclude(machine_id=0).filter(log_file='').order_by('-id').first()
    by_hand  = events.filter(machine_id=0).order_by('-id').first()
    return bench is not None and (by_hand is None or bench.id > by_hand.id)


def match_state(test):
    # (state, sprt.result), derived from the test every time (openbench-reports.md, The fixes)
    if test.deleted:
        return 'stopped', None
    if test.finished and (test.passed or test.failed):
        if test.test_mode != 'SPRT':
            return 'finished', None
        return 'finished', 'passed' if test.passed else 'failed'
    if test.finished:
        return ('error' if stopped_by_bench_error(test) else 'stopped'), None
    return 'running', None


def schema_in_use(schema=None):
    schema = schema or getattr(settings, 'NYAYO_SCHEMA', '') or 'v1'
    if schema not in ('openbench', 'v1'):
        raise ValueError('NYAYO_SCHEMA must be openbench or v1, not %r' % (schema))
    return schema


def build_match_document(test, schema=None):
    if schema_in_use(schema) == 'v1':
        return build_v1_document(test)
    return build_openbench_document(test)


def iso(when):
    return when.isoformat().replace('+00:00', 'Z') if when else None


def uci_options(options):
    # "Threads=1 Hash=16" as { "Threads": 1, "Hash": 16 }
    parsed = {}
    for name, value in re.findall(r'''([^\s=]+)=("[^"]*"|'[^']*'|\S+)''', options or ''):
        value = value.strip('"\'')
        parsed[name] = int(value) if re.fullmatch(r'-?\d+', value) else value
    return parsed


def player_name(engine, branch):
    # What nyayo's pages call a side of the test, and its games' players: the engine and the branch
    return '%s %s' % (engine, branch)


def build_v1_document(test):
    # nyayo's match schema v1, as its docs/match-schema.md (OpenBench) maps a test

    rows  = list(Result.objects.filter(test=test).select_related('machine', 'machine__user').order_by('updated'))
    count = lambda name: sum(getattr(row, name) for row in rows)

    instance           = getattr(settings, 'NYAYO_INSTANCE', 'local')
    state, sprt_result = match_state(test)
    openbench_url      = getattr(settings, 'NYAYO_OPENBENCH_URL', '').strip().rstrip('/')

    finished_at = None
    if state != 'running':
        event       = LogEvent.objects.filter(test_id=test.id).order_by('-id').first()
        finished_at = iso(event.created if event else test.updated)

    sprt = stats = None
    if test.test_mode == 'SPRT':
        model = 'penta' if test.use_penta else 'tri'
        sprt  = {
            'elo0': test.elolower, 'elo1': test.eloupper, 'alpha': test.alpha, 'beta': test.beta, 'model': model,
            'llr': test.currentllr, 'lower': test.lowerllr, 'upper': test.upperllr, 'result': sprt_result,
        }
        # As the test page shows it (templatetags/mytags.py)
        lower, elo, upper = OpenBench.stats.Elo(test.results())
        stats = { 'elo': elo, 'error': max(upper - elo, elo - lower), 'model': model }

    machines = []
    for row in rows:
        name = (row.machine.info or {}).get('machine_name')
        machines.append({
            'name': name if name and name != 'None' else 'machine %d' % (row.machine.id),
            'machine_id': row.machine.id, 'user': row.machine.user.username,
            'games': row.games, 'crashes': row.crashes, 'timeloss': row.timeloss, 'updated_at': iso(row.updated),
        })

    return {
        'schema_version': 1,
        'match': {
            'id': build_match_id(test),
            'name': '%s vs %s' % (player_name(test.dev_engine, test.dev.name), test.base.name),
            'kind': 'sprt' if test.test_mode == 'SPRT' else 'games',
            'state': state,
            'producer': {
                'name': 'OpenBench', 'version': instance,
                'url': '%s/test/%d/' % (openbench_url, test.id) if openbench_url else None,
            },
            'started_at': iso(test.creation),
            'updated_at': iso(timezone.now()),
            'finished_at': finished_at,
            'host': { 'name': instance },
            'settings': {
                'tc': test.dev_time_control, 'book': test.book_name,
                'runner': { 'name': 'cutechess-ob' }, 'pgn_options': [],
                # Fixed once nyayo has the match, so only what a test can't change: not its
                # workload size (MODIFY), upload_pgns or scale_nps (both set at a first workload)
                'openbench': {
                    'test_mode': test.test_mode, 'max_games': test.max_games,
                    'base_time_control': test.base_time_control,
                    'win_adj': test.win_adj, 'draw_adj': test.draw_adj,
                    'syzygy_wdl': test.syzygy_wdl, 'syzygy_adj': test.syzygy_adj,
                    'scale_method': test.scale_method,
                    'dev_network': test.dev_netname, 'base_network': test.base_netname,
                },
            },
        },
        'subject': {
            'name': test.dev_engine, 'version': '', 'commit': test.dev.sha, 'branch': test.dev.name,
            'repo': test.dev_repo, 'bench': test.dev.bench, 'options': uci_options(test.dev_options),
        },
        'opponents': [{
            'id': 'base', 'name': test.base_engine, 'version': '', 'commit': test.base.sha, 'branch': test.base.name,
            'repo': test.base_repo, 'bench': test.base.bench, 'options': uci_options(test.base_options),
            'rating': None,
        }],
        'results': [{
            'opponent': 'base',
            'games': count('games'), 'wins': count('wins'), 'draws': count('draws'), 'losses': count('losses'),
            'penta': [count('LL'), count('LD'), count('DD'), count('DW'), count('WW')],
            # OpenBench doesn't know which side crashed or lost on time
            'timelosses': { 'subject': count('timeloss'), 'opponent': 0 },
            'crashes': { 'subject': count('crashes'), 'opponent': 0 },
        }],
        'progress': None,
        'games': None,
        'sprt': sprt,
        'stats': stats,
        'machines': machines,
    }


def build_openbench_document(test, producer_url=None):
    if producer_url is None:
        producer_url = '/test/%d/' % test.id

    result_rows = Result.objects.filter(test=test).order_by('updated')
    totals = {
        'games': 0,
        'wins': 0,
        'losses': 0,
        'draws': 0,
        'LL': 0,
        'LD': 0,
        'DD': 0,
        'DW': 0,
        'WW': 0,
        'crashes': 0,
        'timeloss': 0,
    }

    for row in result_rows:
        totals['games'] += row.games
        totals['wins'] += row.wins
        totals['losses'] += row.losses
        totals['draws'] += row.draws
        totals['LL'] += row.LL
        totals['LD'] += row.LD
        totals['DD'] += row.DD
        totals['DW'] += row.DW
        totals['WW'] += row.WW
        totals['crashes'] += row.crashes
        totals['timeloss'] += row.timeloss

    kind = 'sprt' if test.test_mode == 'SPRT' else 'games'
    state, sprt_result = match_state(test)

    document = {
        'schema_version': 1,
        'id': build_match_id(test),
        'kind': kind,
        'state': state,
        'updated_at': timezone.now().isoformat().replace('+00:00', 'Z'),
        'producer': {
            'kind': 'OpenBench',
            'url': producer_url,
            'instance': getattr(settings, 'NYAYO_INSTANCE', 'local'),
        },
        'subject': {
            'engine': test.dev.name,
            'name': test.dev.name,
            'repo': test.dev_repo,
            'sha': test.dev.sha,
            'bench': test.dev.bench,
            'options': test.dev_options,
            'time_control': test.dev_time_control,
            'network': test.dev_netname,
            'book': test.book_name,
        },
        'opponents': [{
            'engine': test.base.name,
            'name': test.base.name,
            'repo': test.base_repo,
            'sha': test.base.sha,
            'bench': test.base.bench,
            'options': test.base_options,
            'time_control': test.base_time_control,
            'network': test.base_netname,
        }],
        'results': [{
            'games': totals['games'],
            'wins': totals['wins'],
            'losses': totals['losses'],
            'draws': totals['draws'],
            'LL': totals['LL'],
            'LD': totals['LD'],
            'DD': totals['DD'],
            'DW': totals['DW'],
            'WW': totals['WW'],
            'crashes': totals['crashes'],
            'timeloss': totals['timeloss'],
        }],
        'sprt': {
            'lower': test.elolower,
            'upper': test.eloupper,
            'alpha': test.alpha,
            'beta': test.beta,
            'current_llr': test.currentllr,
            'lower_llr': test.lowerllr,
            'upper_llr': test.upperllr,
            'result': sprt_result,
        },
        'settings': {
            'dev_time_control': test.dev_time_control,
            'base_time_control': test.base_time_control,
            'book_name': test.book_name,
            'adjudication': test.win_adj,
            'syzygy_wdl': test.syzygy_wdl,
            'syzygy_adj': test.syzygy_adj,
            'scale_method': test.scale_method,
            'scale_nps': test.scale_nps,
        },
        'machines': [],
        'error': bool(test.error),
        'finished': bool(test.finished),
    }

    for row in result_rows:
        document['machines'].append({
            'machine_id': row.machine.id,
            'user': row.machine.user.username,
            'games': row.games,
            'wins': row.wins,
            'losses': row.losses,
            'draws': row.draws,
            'crashes': row.crashes,
            'timeloss': row.timeloss,
            'updated_at': row.updated.isoformat().replace('+00:00', 'Z'),
        })

    return document


def enqueue_report(test_id):
    if not nyayo_enabled():
        return False
    REPORT_QUEUE.put(test_id)
    return True


def enqueue_pgn(test_id, result_id, book_index, data):
    # A worker's uploaded batch (.pgn.bz2), on the games' queue: in order, behind no report
    if not nyayo_enabled():
        return False
    GAME_QUEUE.put((test_id, result_id, book_index, data))
    return True


def nyayo_opponent_id(name):
    # The id nyayo gives the test's one opponent, from its name (nyayo/matches/ingest.py, safe_id)
    text = re.sub(r'[^A-Za-z0-9._-]+', '-', str(name or '').strip()).strip('._-') or 'opponent-1'
    if not text[0].isalnum():
        text = 'x-%s' % (text)
    return text[:64]


def split_games(text):
    # A stripped batch is "<headers>\n\n<moves>\n\n" per game (Client/pgn_util.py)
    games, headers = [], None
    for block in re.split(r'\n\s*\n', text.strip()):
        block = block.strip()
        if block.startswith('['):
            headers = block
        elif headers is not None and block:
            games.append((headers, block))
            headers = None
    return games


def tag_game(headers, moves, subject_colour, opponent):
    # Which player is the engine under test: cutechess names them <engine>-dev and <engine>-base
    tags = headers
    if subject_colour:
        tags += '\n[Subject "%s"]' % (subject_colour)
    if opponent:
        tags += '\n[Opponent "%s"]' % (opponent)
    return '%s\n\n%s\n' % (tags, moves)


def rename_players(headers, dev, base):
    # cutechess's <engine>-dev and <engine>-base, as the sides' names in the match document
    def rename(match):
        name = match.group(2)
        name = dev if name.endswith('-dev') else base if name.endswith('-base') else name
        return '[%s "%s"]' % (match.group(1), name.replace('"', "'"))
    return re.sub(r'^\[(White|Black) "([^"]*)"\]', rename, headers, flags=re.M)


def subject_colour(headers):
    white = re.search(r'^\[White "([^"]*)"\]', headers, re.M)
    black = re.search(r'^\[Black "([^"]*)"\]', headers, re.M)
    if white and white.group(1).endswith('-dev'):
        return 'white'
    if black and black.group(1).endswith('-dev'):
        return 'black'
    return ''


def report_pgn(test_id, result_id, book_index, data, schema=None):
    # Each finished game of the batch, PUT to /api/matches/<id>/games/<result>.<book_index>.<n>
    if not nyayo_enabled():
        return False

    try:
        test     = Test.objects.get(id=test_id)
        match_id = build_match_id(test)
        games    = split_games(bz2.decompress(data).decode('utf-8'))
        v1       = schema_in_use(schema) == 'v1'
        opponent = 'base' if v1 else nyayo_opponent_id(test.base.name)
        names    = (player_name(test.dev_engine, test.dev.name), player_name(test.base_engine, test.base.name))
        headers  = {
            'Authorization': 'Bearer %s' % settings.NYAYO_TOKEN,
            'Content-Type': 'application/x-chess-pgn; charset=utf-8',
        }
    except Exception:
        logger.exception('OpenBench failed to read the PGNs of test %s for nyayo', test_id)
        return False

    sent = 0
    with requests.Session() as session:
        for number, (tags, moves) in enumerate(games, 1):
            game = '%d.%d.%d' % (result_id, book_index, number)
            url  = '%s/api/matches/%s/games/%s' % (settings.NYAYO_URL.rstrip('/'), match_id, game)
            colour = subject_colour(tags)
            body   = tag_game(rename_players(tags, *names) if v1 else tags, moves, colour, opponent).encode('utf-8')
            try:
                response = session.put(url, data=body, headers=headers, timeout=5)
                # nyayo doesn't know the match (it was down for its reports): send it, then the game again
                if response.status_code == 404 and report_test(test_id, schema):
                    response = session.put(url, data=body, headers=headers, timeout=5)
                if refused(response):
                    logger.error('nyayo refused game %s of test %s: %s', game, test_id, refused(response))
                    continue
                sent += 1
            except Exception:
                logger.exception('OpenBench failed to report game %s of test %s to nyayo', game, test_id)

    return sent == len(games)


def report_test(test_id, schema=None):
    if not nyayo_enabled():
        return False

    try:
        test = Test.objects.get(id=test_id)
        document = build_match_document(test, schema)
        url = '%s/api/matches/%s' % (settings.NYAYO_URL.rstrip('/'), build_match_id(test))
        response = requests.put(
            url,
            json=document,
            headers={
                'Authorization': 'Bearer %s' % settings.NYAYO_TOKEN,
                'Content-Type': 'application/json',
            },
            timeout=5,
        )
        if refused(response):
            logger.error('nyayo refused the match of test %s: %s', test_id, refused(response))
            return False
        return True
    except Exception:
        logger.exception('OpenBench failed to report match for test %s to nyayo', test_id)
        return False


def refused(response):
    # nyayo's answer when it isn't 200 or 201: the status and its plain-text reason, which says what to fix
    if response.status_code in (200, 201):
        return None
    return '%d %s' % (response.status_code, response.text.strip()[:500])


def _report_loop():
    while not REPORT_STOP.is_set():
        try:
            test_id = REPORT_QUEUE.get(timeout=1)
        except queue.Empty:
            continue
        report_test(test_id)


def _game_loop():
    # A game whose match nyayo doesn't know yet has the match sent first, from this thread (report_pgn)
    while not REPORT_STOP.is_set():
        try:
            item = GAME_QUEUE.get(timeout=1)
        except queue.Empty:
            continue

        try:
            report_pgn(*item)
        finally:
            GAME_QUEUE.task_done()


def start_report_loop():
    global REPORT_THREAD, GAME_THREAD

    if not nyayo_enabled():
        return None

    if REPORT_THREAD is not None and REPORT_THREAD.is_alive():
        return REPORT_THREAD

    REPORT_STOP.clear()
    REPORT_THREAD = threading.Thread(target=_report_loop, name='nyayo-report-loop', daemon=True)
    GAME_THREAD   = threading.Thread(target=_game_loop, name='nyayo-game-loop', daemon=True)
    REPORT_THREAD.start()
    GAME_THREAD.start()
    return REPORT_THREAD


def stop_report_loop():
    REPORT_STOP.set()
