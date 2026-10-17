# A toy UCI engine for the OpenBench smoke test. It plays a random legal move,
# seeded by the position, so a game is reproducible. `bench` always prints BENCH;
# a bench mismatch is a test whose expected bench differs. The CrashAfter option
# makes it exit without a word after N moves (0: never), for crash counts.
#
# The makefile turns this file into the executable the worker builds.

import hashlib
import os
import random
import sys

import chess

BENCH = 1000000
NPS   = 1000000

def bench():
    print('%d nodes %d nps' % (BENCH, NPS), flush=True)

def choose_move(board, seed):
    moves = sorted(board.legal_moves, key=lambda move: move.uci())
    # Mates and captures first, so random games end before the 50-move rule
    for move in moves:
        board.push(move)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return move
    captures = [move for move in moves if board.is_capture(move)]
    digest   = hashlib.sha256(('%s %d' % (board.fen(), seed)).encode()).digest()
    rng      = random.Random(digest)
    return rng.choice(captures if captures and rng.random() < 0.7 else moves)

def parse_position(tokens):
    board = chess.Board()
    if tokens[1:2] == ['fen']:
        end   = tokens.index('moves') if 'moves' in tokens else len(tokens)
        board = chess.Board(' '.join(tokens[2:end]))
    if 'moves' in tokens:
        for uci in tokens[tokens.index('moves') + 1:]:
            board.push_uci(uci)
    return board

def uci_loop():
    board, played = chess.Board(), 0
    options       = { 'CrashAfter' : 0, 'Seed' : 0 }

    for line in sys.stdin:
        tokens = line.split()
        if not tokens:
            continue

        if tokens[0] == 'uci':
            print('id name smoke')
            print('id author OpenBench smoke test')
            print('option name Threads type spin default 1 min 1 max 1')
            print('option name Hash type spin default 1 min 1 max 1024')
            print('option name CrashAfter type spin default 0 min 0 max 100000')
            print('option name Seed type spin default 0 min 0 max 100000')
            print('uciok', flush=True)

        elif tokens[0] == 'isready':
            print('readyok', flush=True)

        elif tokens[0] == 'setoption' and 'name' in tokens and 'value' in tokens:
            name  = ' '.join(tokens[tokens.index('name') + 1:tokens.index('value')])
            value = ' '.join(tokens[tokens.index('value') + 1:])
            if name in options:
                options[name] = int(value)

        elif tokens[0] == 'ucinewgame':
            board = chess.Board()

        elif tokens[0] == 'position':
            board = parse_position(tokens)

        elif tokens[0] == 'go':
            if options['CrashAfter'] and played >= options['CrashAfter']:
                os._exit(1)
            move    = choose_move(board, options['Seed'])
            played += 1
            print('info depth 1 nodes 1 score cp 0 pv %s' % (move.uci()))
            print('bestmove %s' % (move.uci()), flush=True)

        elif tokens[0] == 'bench':
            bench()

        elif tokens[0] == 'quit':
            break

if __name__ == '__main__':
    if sys.argv[1:2] == ['bench']:
        bench()
    else:
        uci_loop()
