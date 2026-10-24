#!/bin/python3

# The fork's workers upload each game with its final players and tags (nyayo's game database):
#   - pgn_util names <engine>-dev and <engine>-base <engine> <branch>, tags Subject and Opponent, keeps Termination
#   - cutechess still plays under <engine>-dev and <engine>-base, so the result counts are unchanged
#   - the reporter sends a game already tagged as it is, and still renames and tags an older one, once
# Run from UnitTests/ with the Client's requirements installed (Client/requirements.txt).

import importlib.util
import os
import sys
import types

# Needed to include from ../Client/*.py
PARENT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir))
sys.path.append(os.path.join(PARENT, 'Client'))

import pgn_util

PLAYERS = { 'dev' : 'Weiss feature "x"', 'base' : 'Weiss master' }

def tags_of(text):
    return pgn_util.pgn_header_list(x for x in text.split('\n') if x)

def test_names_and_tags():

    games = list(pgn_util.pgn_iterator('example3.pgn'))
    assert [h['White'] for h, _ in games[:2]] == ['Weiss-dev', 'Weiss-base']

    for headers, _ in games:
        headers['Termination'] = 'adjudication'
        dev_white = headers['White'].endswith('-dev')
        for compact in (True, False):

            tags = tags_of(pgn_util.pgn_strip_headers(dict(headers), compact, PLAYERS))
            dev, base = "Weiss feature 'x'", 'Weiss master'  # A quote in a name becomes ', as the reporter does
            assert (tags['White'], tags['Black']) == ((dev, base) if dev_white else (base, dev)), tags
            assert tags['Subject'] == ('white' if dev_white else 'black'), tags
            assert tags['Opponent'] == 'base', tags
            assert tags['Termination'] == 'adjudication', tags

            # Without players, as upstream: cutechess's names, no tags of the fork's, Termination kept
            plain = tags_of(pgn_util.pgn_strip_headers(dict(headers), compact))
            assert plain['White'] == headers['White'] and 'Subject' not in plain and 'Opponent' not in plain
            assert plain['Termination'] == 'adjudication'

    # Subject and Opponent come last, after every tag upstream keeps
    order = [x.split()[0][1:] for x in pgn_util.pgn_strip_headers(dict(games[0][0]), False, PLAYERS).split('\n')]
    assert order[-2:] == ['Subject', 'Opponent'], order

def test_counts_unchanged():

    # cutechess is still given <engine>-<side>: update_results reads its lines by word position
    sys.argv = sys.argv[:1]
    import worker

    workload = { 'test' : { 'type' : 'GAMES', 'syzygy_wdl' : 'DISABLED' } }
    for side in ('dev', 'base'):
        workload['test'][side] = {
            'options' : 'Threads=1 Hash=16', 'network' : None, 'private' : False,
            'engine' : 'Weiss', 'name' : 'feature x', 'time_control' : 'N=1000',
        }
    config = types.SimpleNamespace(workload=workload)
    for side in ('dev', 'base'):
        settings = worker.Cutechess.engine_settings(config, 'Weiss-%s' % (side), side, 1.0, 0)
        assert settings.endswith(' name=Weiss-%s' % (side)), settings

    lines = [
        'Finished game 1 (Weiss-dev vs Weiss-base): 1-0 {White mates}',
        'Finished game 2 (Weiss-base vs Weiss-dev): 1/2-1/2 {Draw by adjudication}',
        'Finished game 3 (Weiss-dev vs Weiss-base): 0-1 {White loses on time}',
        'Finished game 4 (Weiss-base vs Weiss-dev): 0-1 {Black mates}',
    ]
    results = { 'games' : {}, 'trinomial' : [0, 0, 0], 'pentanomial' : [0] * 5,
                'crashes' : 0, 'timelosses' : 0, 'illegals' : 0 }
    for line in lines:
        worker.Cutechess.update_results(results, line)

    assert results['trinomial'] == [1, 1, 2], results     # L, D, W from dev's side
    assert results['pentanomial'] == [0, 0, 1, 1, 0], results
    assert results['timelosses'] == 1 and results['crashes'] == 0, results

def load_reporter():

    # OpenBench/nyayo.py without a Django project: only its game functions are used
    for name in ('OpenBench', 'OpenBench.stats', 'OpenBench.models'):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules['OpenBench'].__path__ = []
    for model in ('LogEvent', 'Result', 'Test'):
        setattr(sys.modules['OpenBench.models'], model, None)

    spec   = importlib.util.spec_from_file_location('nyayo_reporter', os.path.join(PARENT, 'OpenBench', 'nyayo.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_reporter_once():

    nyayo = load_reporter()
    names = ('Weiss feature x', 'Weiss master')

    with open('example3.pgn') as fin:
        older = nyayo.split_games(fin.read())[0]
    tags, moves = older

    # An older upload, cutechess's names: renamed and tagged on the server
    body = nyayo.game_body(tags, moves, names, True, 'base')
    sent = tags_of(body.split('\n\n')[0])
    assert (sent['White'], sent['Black'], sent['Subject'], sent['Opponent']) == (*names, 'white', 'base'), sent

    # Sent again, or uploaded named and tagged by the worker: as it is, no second rename or tag
    tags, moves = nyayo.split_games(body)[0]
    assert nyayo.game_body(tags, moves, names, True, 'base') == body
    assert body.count('[Subject ') == 1 and body.count('[Opponent ') == 1

    headers = pgn_util.pgn_header_list(x for x in tags.split('\n') if x)
    headers['White'], headers['Black'] = 'Weiss-dev', 'Weiss-base'
    worker_tags = pgn_util.pgn_strip_headers(headers, True, { 'dev' : names[0], 'base' : names[1] })
    assert nyayo.game_body(worker_tags, moves, ('wrong', 'names'), True, 'other') == '%s\n\n%s\n' % (worker_tags, moves)

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    for test in (test_names_and_tags, test_counts_unchanged, test_reporter_once):
        test()
        print('PASS', test.__name__)
