# What nyayo should get after each event, and the checks run over a scenario's reports.
#
#   Event                                  nyayo should get
#   results  (clientSubmitResults)         one report, counts equal to the Test after it, LLR equal to
#                                          test.currentllr; finished (with sprt.result) once the test ends.
#                                          None when the test was already finished or deleted (the worker
#                                          is told to stop and nothing changes).
#   STOP     (modifyWorkload)              stopped
#   DELETE                                 stopped
#   RESTORE                                the state it had before the delete
#   APPROVE, MODIFY                        a report, state unchanged
#   bench_error  (clientBenchError)        error
#   backfill (report_to_nyayo)             one report, the test as it stands
#   pgn      (clientSubmitPGN)             no match report; one PUT .../games/<result>.<book_index>.<n> per
#                                          game in the batch, the game as uploaded, after the match exists in
#                                          nyayo. The worker uploads each game named <engine> <branch> and
#                                          tagged Subject and Opponent "base", cutechess's Termination kept
#
# An event while nyayo is down has no report (lost); the next report carries the whole state.
#
# Reports and games go on queues of their own, each drained by its own thread:
#   - a test's report is queued once: an event while its report still waits has no report of its
#     own (superseded), the waiting one is built when sent and carries both
#   - a game nyayo answers 404 (its match not there yet) has the match sent first, an extra report
#     of the test as it stands (a resend), then the game again
#   - a game nyayo answers 503 (busy) is sent again after its Retry-After, and arrives once
#   - a report never waits for a game: with two tests at once, the second's first report arrives
#     while the first's games are still being sent (check_status_first)
#
# The states are openbench-reports.md's ("The fixes"), not what OpenBench/nyayo.py does today.

import bz2
import collections
import math
import re

COUNTS = ('games', 'wins', 'losses', 'draws', 'LL', 'LD', 'DD', 'DW', 'WW')

# Scenarios that fail today, with the plan item that fixes each and the runs it applies to.
# The run passes while they fail and fails when one starts passing: remove the mark in the
# commit that fixes it.
WITH_NYAYO = lambda args: bool(args.nyayo)
V1_NYAYO   = lambda args: bool(args.nyayo) and args.schema == 'v1'
XFAIL = {
}


def xfail(key, args):
    reason, applies = XFAIL.get(key, (None, None))
    return reason if reason and applies(args) else None


class Event:

    def __init__(self, t, kind, test_id, before, after, report, state, lost):
        self.t, self.kind, self.test_id, self.lost = t, kind, test_id, lost
        self.before, self.after = before, after
        self.report, self.state = report, state

    def __repr__(self):
        outcome = self.state if self.report else ('%s, if it arrives' % (self.state) if self.lost else 'no report')
        return '%s(test %d at %s games -> %s)' % (self.kind, self.test_id, self.after['games'], outcome)


def finished_state(row):
    if row['deleted']:
        return 'stopped'
    if row['finished'] and (row['passed'] or row['failed']):
        return 'finished'
    return None


class Expectations:

    def __init__(self, instance):
        self.instance      = instance
        self.events        = []
        self.state         = {}  # test id -> the state its last report should have had
        self.before_delete = {}

    def record(self, t, kind, test_id, before, after, lost=False):
        previous = self.state.get(test_id, 'running')
        report   = True

        if kind == 'results':
            report = not (before['finished'] or before['deleted'])
            state  = finished_state(after) or previous
        elif kind == 'bench_error':
            state  = 'error'
        elif kind in ('STOP', 'DELETE'):
            if kind == 'DELETE': self.before_delete[test_id] = previous
            state  = 'stopped'
        elif kind == 'RESTORE':
            state  = self.before_delete.pop(test_id, previous)
        elif kind in ('APPROVE', 'MODIFY'):
            state  = previous
        elif kind == 'pgn':
            report, state = False, previous
        elif kind in ('backfill', 'backfill-v1'):
            state  = finished_state(after) or previous
        else:
            raise ValueError('no expectation for %s' % (kind))

        if report:
            self.state[test_id] = state

        event = Event(t, kind, test_id, before, after, report and not lost, state, lost and report)
        self.events.append(event)
        return event

    def expected(self, test_id):
        return [x for x in self.events if x.test_id == test_id and x.report]

    def check(self, test_id, reports, final, results, engines=None, resends=0):
        # Returns the problems found: nothing missing, nothing extra, always consistent.
        # resends: how many of the test's games nyayo answered 404, each worth one report more

        problems = []
        match_id = 'openbench-%s-%d' % (self.instance, test_id)

        # An event while nyayo was down may still have its report arrive: the reporter sends
        # from its queue after the event, maybe once nyayo is back. Such a report is optional,
        # and so is one superseded: the next event came before this report arrived, so it may
        # have found the report still waiting. Reports are aligned to events, skipping optional
        # events they don't match; one repeating the report before it may be a resend.
        events = [x for x in self.events if x.test_id == test_id and x.kind != 'pgn' and (x.report or x.lost)]
        optional = lambda index, report: events[index].lost or (index + 1 < len(events) and events[index + 1].t < report['t'])
        pairs, extra, position = [], 0, 0
        for report in reports:
            index = position
            while index < len(events) and not matches(events[index], report) and optional(index, report):
                index += 1
            if resends and pairs and (index == len(events) or not matches(events[index], report)) \
                    and same_state(pairs[-1][1], report):
                resends -= 1
                continue
            if index == len(events):
                extra += 1
                continue
            pairs.append((events[index], report))
            position = index + 1

        missing = [x for x in events[position:] if not x.lost]
        if missing:
            problems.append('missing %d report(s), for %s' % (len(missing), missing))
        if extra:
            problems.append('%d report(s) no event explains' % (extra))
        if problems or any(x.lost for x in events):
            problems_detail = 'events: %s' % (events)
        else:
            problems_detail = None

        for index, (event, report) in enumerate(pairs):
            doc, where = view(report['document']), 'report %d (after %s)' % (index + 1, event.kind)

            if doc['id'] != match_id:
                problems.append('%s: id %r, expected %r' % (where, doc['id'], match_id))
            if doc['state'] != event.state:
                problems.append('%s: state %r, expected %r' % (where, doc['state'], event.state))
            if doc['kind'] != ('sprt' if final['test_mode'] == 'SPRT' else 'games'):
                problems.append('%s: kind %r for a %s test' % (where, doc['kind'], final['test_mode']))
            if event.kind == 'backfill-v1' and not doc['v1']:
                problems.append('%s: not a version 1 document' % (where))

            if event.kind in ('results', 'backfill', 'backfill-v1'):
                want = { name : event.after[name] for name in COUNTS }
                if doc['counts'] != want:
                    problems.append('%s: counts %s, the Test had %s' % (where, doc['counts'], want))

                if final['test_mode'] == 'SPRT':
                    llr = doc['llr']
                    if llr is None or not math.isclose(llr, event.after['currentllr'], abs_tol=1e-9):
                        problems.append('%s: LLR %r, the Test had %r' % (where, llr, event.after['currentllr']))

                if doc['v1']:
                    problems += ['%s: %s' % (where, x) for x in check_v1(report['document'], event.after, engines)]

            if final['test_mode'] == 'SPRT' and event.state == 'finished':
                want = 'passed' if event.after['passed'] else 'failed'
                if doc['sprt_result'] != want:
                    problems.append('%s: sprt.result %r, expected %r' % (where, doc['sprt_result'], want))

        # Counts never go down
        for index, (older, newer) in enumerate(zip(reports, reports[1:])):
            old, new = view(older['document'])['counts'], view(newer['document'])['counts']
            fallen = [name for name in COUNTS if new[name] < old[name]]
            if fallen:
                problems.append('report %d: %s went down' % (index + 2, ', '.join(fallen)))

        # The last report agrees with OpenBench's Test row
        if reports:
            doc  = view(reports[-1]['document'])
            want = { name : final[name] for name in COUNTS }
            if doc['counts'] != want:
                problems.append('last report: counts %s, the Test has %s' % (doc['counts'], want))
            crashes = sum(row['crashes'] for row in results)
            got     = doc['crashes']
            if got != crashes:
                problems.append('last report: %d crashes, the Results have %d' % (got, crashes))

        if problems and problems_detail:
            problems.append(problems_detail)
        return problems


def uploaded_games(event):
    # The games of an uploaded batch, as {game id: text}, split on [Event independently of the server
    text   = bz2.decompress(event.upload).decode('utf-8').strip()
    chunks = [x.strip() for x in re.split(r'\n\s*\n(?=\[Event )', text) if x.strip()]
    return { '%s.%s.%d' % (event.result_id, event.book_index, n) : chunk for n, chunk in enumerate(chunks, 1) }


def normal(pgn):
    return '\n'.join(line.rstrip() for line in pgn.strip().splitlines() if line.strip())


def check_games(events, games, reports, names=None, endings=None):
    # Every uploaded game left the worker named and tagged, and reached nyayo once, as uploaded, after its match

    # Returns (problems, how many games must have arrived); a batch uploaded while nyayo was
    # down may be missing, since nothing resends it yet. names: (dev, base) as the match names them;
    # endings: { game : cutechess's Termination, or None }, from the worker's own PGNs

    problems, want, optional = [], {}, set()
    for event in events:
        if event.kind == 'pgn':
            batch = uploaded_games(event)
            want.update(batch)
            if event.lost:
                optional.update(batch)

    got     = collections.Counter(x['game'] for x in games)
    missing = sorted(set(want) - set(got) - optional)
    extra   = sorted(set(got) - set(want))
    twice   = sorted(x for x, count in got.items() if count > 1)
    if missing: problems.append('%d uploaded game(s) never reached nyayo: %s' % (len(missing), missing[:5]))
    if extra:   problems.append('%d game(s) nobody uploaded: %s' % (len(extra), extra[:5]))
    if twice:   problems.append('game(s) sent more than once: %s' % (twice[:5]))

    # As the worker uploaded it: <engine> <branch>, dev's colour, the opponent, how it ended
    for game, pgn in sorted(want.items()) if names else []:
        tags = dict(re.findall(r'^\[(\w+) "([^"]*)"\]$', pgn, re.M))
        dev, base = names
        colour = tags.get('Subject')
        if colour not in ('white', 'black'):
            problems.append('uploaded game %s: Subject %r' % (game, colour))
            continue
        players = { 'White' : dev, 'Black' : base } if colour == 'white' else { 'White' : base, 'Black' : dev }
        if { k : tags.get(k) for k in ('White', 'Black') } != players:
            problems.append('uploaded game %s: players %s, expected %s' % (
                game, { k : tags.get(k) for k in ('White', 'Black') }, players))
        if tags.get('Opponent') != 'base':
            problems.append('uploaded game %s: Opponent %r, expected base' % (game, tags.get('Opponent')))
        if endings is not None and tags.get('Termination') != endings.get(game):
            problems.append('uploaded game %s: Termination %r, cutechess wrote %r' % (
                game, tags.get('Termination'), endings.get(game)))

    first_report = min((x['t'] for x in reports), default=None)
    for sent in games:
        if sent['game'] not in want:
            continue
        # The reporter renames and tags nothing a second time
        if normal(sent['pgn']) != normal(want[sent['game']]):
            problems.append('game %s: differs from the uploaded game' % (sent['game']))
        if first_report is None or sent['t'] < first_report:
            problems.append('game %s: sent before its match' % (sent['game']))

    return problems, len(set(want) - optional)


def check_status_first(first, second, first_events, first_games, second_events, second_reports):
    # The second test's first report arrived while games of the first, uploaded before that
    # report was queued, were still to be answered: status didn't wait behind games

    queued = min((x.t for x in second_events if x.report), default=None)
    if queued is None or not second_reports:
        return ['test %d: no report arrived' % (second)]
    arrived  = min(x['t'] for x in second_reports)
    uploaded = { game : event.t for event in first_events if event.kind == 'pgn' for game in uploaded_games(event) }
    earlier  = [x for x in first_games if uploaded.get(x['game'], queued) < queued]
    if any(x['t'] > arrived for x in earlier):
        return []
    return ['the first report of test %d arrived %.2fs after it was queued, after all %d game(s) of test %d '
            'uploaded before it: status waited behind games' % (second, arrived - queued, len(earlier), first)]


def same_state(older, newer):
    a, b = view(older['document']), view(newer['document'])
    return (a['state'], a['counts']) == (b['state'], b['counts'])


def matches(event, report):
    doc = view(report['document'])
    if doc['state'] != event.state:
        return False
    return event.kind != 'results' or doc['counts'] == { name : event.after[name] for name in COUNTS }


def view(doc):
    # The same facts from either document: OpenBench's own shape, or match schema v1
    if 'match' in doc:
        row   = (doc.get('results') or [{}])[0]
        penta = row.get('penta') or [None] * 5
        sprt  = doc.get('sprt') or {}
        return {
            'v1' : True, 'id' : doc['match'].get('id'), 'kind' : doc['match'].get('kind'),
            'state' : doc['match'].get('state'), 'llr' : sprt.get('llr'), 'sprt_result' : sprt.get('result'),
            'counts' : { **{ name : row.get(name) for name in ('games', 'wins', 'losses', 'draws') },
                         **dict(zip(('LL', 'LD', 'DD', 'DW', 'WW'), penta)) },
            'crashes' : sum((row.get('crashes') or {}).values()),
        }
    rows = doc.get('results') or [{}]
    sprt = doc.get('sprt') or {}
    return {
        'v1' : False, 'id' : doc.get('id'), 'kind' : doc.get('kind'), 'state' : doc.get('state'),
        'llr' : sprt.get('current_llr'), 'sprt_result' : sprt.get('result'),
        'counts' : { name : rows[0].get(name) for name in COUNTS },
        'crashes' : sum(x.get('crashes', 0) for x in rows),
    }


def document_counts(doc):
    return view(doc)['counts']


def check_v1(doc, test, engines):
    # A version 1 document as nyayo's docs/match-schema.md (OpenBench) maps the test
    from OpenBench import stats

    problems = []
    dev, base = engines['dev'], engines['base']
    subject, opponents = doc.get('subject') or {}, doc.get('opponents') or []

    if (subject.get('name'), subject.get('branch'), subject.get('commit')) != (test['dev_engine'], dev['name'], dev['sha']):
        problems.append('subject %s, the dev engine is %s %s %s' % (
            [subject.get(x) for x in ('name', 'branch', 'commit')], test['dev_engine'], dev['name'], dev['sha']))
    if len(opponents) != 1 or (opponents[0].get('id'), opponents[0].get('name'), opponents[0].get('branch')) \
            != ('base', test['base_engine'], base['name']):
        problems.append('opponents %s, expected one, base: %s %s' % (opponents, test['base_engine'], base['name']))
    if [x.get('opponent') for x in doc.get('results') or []] != ['base']:
        problems.append('results are not one row for base')

    if test['test_mode'] == 'SPRT':
        counts = (test['losses'], test['draws'], test['wins']) if test['use_tri'] and not test['use_penta'] \
            else (test['LL'], test['LD'], test['DD'], test['DW'], test['WW'])
        lower, elo, upper = stats.Elo(counts)
        got = doc.get('stats') or {}
        if not (isinstance(got.get('elo'), float) and math.isclose(got['elo'], elo, abs_tol=1e-6)
                and math.isclose(got.get('error', -1), max(upper - elo, elo - lower), abs_tol=1e-6)):
            problems.append('stats %s, OpenBench has Elo %.2f +- %.2f' % (got, elo, max(upper - elo, elo - lower)))
        sprt = doc.get('sprt') or {}
        want = { 'elo0' : test['elolower'], 'elo1' : test['eloupper'], 'lower' : test['lowerllr'], 'upper' : test['upperllr'] }
        if any(sprt.get(k) != v for k, v in want.items()):
            problems.append('sprt %s, the Test has %s' % (sprt, want))
    elif doc.get('stats') is not None:
        problems.append('stats sent for a %s test' % (test['test_mode']))

    return problems


def timeline(test_id, client_log, events, reports):
    # Both timelines side by side: client <-> server on the left, server -> nyayo on the right

    lines = []
    for entry in client_log:
        if str(entry['form'].get('test_id')) == str(test_id):
            left = '%s %s' % (entry['path'].strip('/'), entry.get('status', '?'))
            if entry['path'].startswith('/clientSubmitResults'):
                left += ' %s' % (entry['form'].get('trinomial', ''))
            lines.append((entry['t'], left, ''))
    for event in events:
        if event.test_id == test_id and event.kind not in ('results', 'bench_error'):
            lines.append((event.t, 'harness %s' % (event.kind), ''))
    for report in reports:
        doc = report['document']
        lines.append((report['t'], '', '%s %s games=%s' % (report['status'], doc.get('state'), document_counts(doc)['games'])))

    if not lines:
        return '(nothing recorded)'

    start = min(t for t, _, _ in lines)
    out = ['%8s  %-52s  %s' % ('t', 'client <-> server', 'server -> nyayo')]
    for t, left, right in sorted(lines, key=lambda x: x[0]):
        out.append('%8.2f  %-52s  %s' % (t - start, left, right))
    return '\n'.join(out)
