import bz2
import logging
import queue
import re
import threading

import requests
from django.conf import settings
from django.utils import timezone

from OpenBench.models import Result, Test

logger = logging.getLogger(__name__)

REPORT_QUEUE = queue.Queue()
REPORT_STOP = threading.Event()
REPORT_THREAD = None


def nyayo_enabled():
    return bool(
        getattr(settings, 'NYAYO_URL', '').strip()
        and getattr(settings, 'NYAYO_TOKEN', '').strip()
        and getattr(settings, 'NYAYO_INSTANCE', '').strip()
    )


def build_match_id(test):
    instance = getattr(settings, 'NYAYO_INSTANCE', 'local')
    return 'openbench-%s-%s' % (instance, test.id)


def build_match_document(test, producer_url=None):
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

    if test.test_mode == 'SPRT':
        kind = 'sprt'
        if test.finished and test.passed:
            state = 'finished'
            sprt_result = 'passed'
        elif test.finished and test.failed:
            state = 'finished'
            sprt_result = 'failed'
        elif test.finished and not test.passed and not test.failed:
            state = 'error' if test.error else 'stopped'
            sprt_result = None
        else:
            state = 'running'
            sprt_result = None
    else:
        kind = 'games'
        if test.finished:
            state = 'finished' if (test.passed or test.failed) else 'stopped'
        else:
            state = 'running'
        sprt_result = None

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
    # A worker's uploaded batch (.pgn.bz2), queued behind the match report of its results
    if not nyayo_enabled():
        return False
    REPORT_QUEUE.put(('pgn', test_id, result_id, book_index, data))
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


def subject_colour(headers):
    white = re.search(r'^\[White "([^"]*)"\]', headers, re.M)
    black = re.search(r'^\[Black "([^"]*)"\]', headers, re.M)
    if white and white.group(1).endswith('-dev'):
        return 'white'
    if black and black.group(1).endswith('-dev'):
        return 'black'
    return ''


def report_pgn(test_id, result_id, book_index, data):
    # Each finished game of the batch, PUT to /api/matches/<id>/games/<result>.<book_index>.<n>
    if not nyayo_enabled():
        return False

    try:
        test     = Test.objects.get(id=test_id)
        match_id = build_match_id(test)
        games    = split_games(bz2.decompress(data).decode('utf-8'))
        opponent = nyayo_opponent_id(test.base.name)
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
            body = tag_game(tags, moves, subject_colour(tags), opponent).encode('utf-8')
            try:
                response = session.put(url, data=body, headers=headers, timeout=5)
                # nyayo doesn't know the match (it was down for its reports): send it, then the game again
                if response.status_code == 404 and report_test(test_id):
                    response = session.put(url, data=body, headers=headers, timeout=5)
                response.raise_for_status()
                sent += 1
            except Exception:
                logger.exception('OpenBench failed to report game %s of test %s to nyayo', game, test_id)

    return sent == len(games)


def report_test(test_id):
    if not nyayo_enabled():
        return False

    try:
        test = Test.objects.get(id=test_id)
        document = build_match_document(test, producer_url='/test/%d/' % test.id)
        url = '%s/api/matches/%s' % (settings.NYAYO_URL.rstrip('/'), document['id'])
        response = requests.put(
            url,
            json=document,
            headers={
                'Authorization': 'Bearer %s' % settings.NYAYO_TOKEN,
                'Content-Type': 'application/json',
            },
            timeout=5,
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception('OpenBench failed to report match for test %s to nyayo', test_id)
        return False


def _report_loop():
    while not REPORT_STOP.is_set():
        try:
            item = REPORT_QUEUE.get(timeout=1)
        except queue.Empty:
            continue

        try:
            if isinstance(item, tuple) and item[0] == 'pgn':
                report_pgn(*item[1:])
            else:
                report_test(item)
        finally:
            REPORT_QUEUE.task_done()


def start_report_loop():
    global REPORT_THREAD

    if not nyayo_enabled():
        return None

    if REPORT_THREAD is not None and REPORT_THREAD.is_alive():
        return REPORT_THREAD

    REPORT_STOP.clear()
    REPORT_THREAD = threading.Thread(target=_report_loop, name='nyayo-report-loop', daemon=True)
    REPORT_THREAD.start()
    return REPORT_THREAD


def stop_report_loop():
    REPORT_STOP.set()
