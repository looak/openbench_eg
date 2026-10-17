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
#                                          game in the batch, the game as uploaded plus Subject and Opponent
#                                          tags, after the match exists in nyayo
#
# An event while nyayo is down has no report (lost); the next report carries the whole state.
#
# The states are openbench-reports.md's ("The fixes"), not what OpenBench/nyayo.py does today.

import bz2
import collections
import math
import re

COUNTS = ('games', 'wins', 'losses', 'draws', 'LL', 'LD', 'DD', 'DW', 'WW')

# Scenarios that fail today, with the plan item that fixes each. The run passes while
# they fail and fails when one starts passing: remove the mark in the commit that fixes it.
XFAIL = {
}


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
        elif kind == 'backfill':
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

    def check(self, test_id, reports, final, results):
        # Returns the problems found: nothing missing, nothing extra, always consistent

        problems = []
        match_id = 'openbench-%s-%d' % (self.instance, test_id)

        # An event while nyayo was down may still have its report arrive: the reporter sends
        # from its queue after the event, maybe once nyayo is back. Such a report is optional,
        # so reports are aligned to events, skipping optional events they don't match.
        events = [x for x in self.events if x.test_id == test_id and x.kind != 'pgn' and (x.report or x.lost)]
        pairs, extra, position = [], 0, 0
        for report in reports:
            index = position
            while index < len(events) and events[index].lost and not matches(events[index], report):
                index += 1
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
            doc, where = report['document'], 'report %d (after %s)' % (index + 1, event.kind)

            if doc.get('id') != match_id:
                problems.append('%s: id %r, expected %r' % (where, doc.get('id'), match_id))
            if doc.get('state') != event.state:
                problems.append('%s: state %r, expected %r' % (where, doc.get('state'), event.state))
            if doc.get('kind') != ('sprt' if final['test_mode'] == 'SPRT' else 'games'):
                problems.append('%s: kind %r for a %s test' % (where, doc.get('kind'), final['test_mode']))

            if event.kind == 'results':
                got = document_counts(doc)
                want = { name : event.after[name] for name in COUNTS }
                if got != want:
                    problems.append('%s: counts %s, the Test had %s' % (where, got, want))

                if final['test_mode'] == 'SPRT':
                    llr = (doc.get('sprt') or {}).get('current_llr')
                    if llr is None or not math.isclose(llr, event.after['currentllr'], abs_tol=1e-9):
                        problems.append('%s: LLR %r, the Test had %r' % (where, llr, event.after['currentllr']))

            if final['test_mode'] == 'SPRT' and event.state == 'finished':
                want = 'passed' if event.after['passed'] else 'failed'
                got  = (doc.get('sprt') or {}).get('result')
                if got != want:
                    problems.append('%s: sprt.result %r, expected %r' % (where, got, want))

        # Counts never go down
        for index, (older, newer) in enumerate(zip(reports, reports[1:])):
            old, new = document_counts(older['document']), document_counts(newer['document'])
            fallen = [name for name in COUNTS if new[name] < old[name]]
            if fallen:
                problems.append('report %d: %s went down' % (index + 2, ', '.join(fallen)))

        # The last report agrees with OpenBench's Test row
        if reports:
            doc  = reports[-1]['document']
            want = { name : final[name] for name in COUNTS }
            if document_counts(doc) != want:
                problems.append('last report: counts %s, the Test has %s' % (document_counts(doc), want))
            crashes = sum(row['crashes'] for row in results)
            got     = sum(row.get('crashes', 0) for row in doc.get('results', []))
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


def check_games(events, games, reports):
    # Every uploaded game reached nyayo once, unchanged but for the two tags, after its match

    # Returns (problems, how many games must have arrived); a batch uploaded while nyayo was
    # down may be missing, since nothing resends it yet

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

    first_report = min((x['t'] for x in reports), default=None)
    for sent in games:
        if sent['game'] not in want:
            continue
        where = 'game %s' % (sent['game'])
        pgn   = sent['pgn']
        tags  = dict(re.findall(r'^\[(Subject|Opponent) "([^"]*)"\]$', pgn, re.M))
        bare  = re.sub(r'^\[(Subject|Opponent) "[^"]*"\]\n', '', pgn, flags=re.M)

        if normal(bare) != normal(want[sent['game']]):
            problems.append('%s: differs from the uploaded game' % (where))
        white  = re.search(r'^\[White "([^"]*)"\]', pgn, re.M)
        colour = 'white' if white and white.group(1).endswith('-dev') else 'black'
        if tags.get('Subject') != colour:
            problems.append('%s: Subject %r, the dev engine plays %s' % (where, tags.get('Subject'), colour))
        if not tags.get('Opponent'):
            problems.append('%s: no Opponent tag' % (where))
        if first_report is None or sent['t'] < first_report:
            problems.append('%s: sent before its match' % (where))

    return problems, len(set(want) - optional)


def matches(event, report):
    doc = report['document']
    if doc.get('state') != event.state:
        return False
    return event.kind != 'results' or document_counts(doc) == { name : event.after[name] for name in COUNTS }


def document_counts(doc):
    rows = doc.get('results') or [{}]
    return { name : rows[0].get(name) for name in COUNTS }


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
