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
#
# An event while nyayo is down has no report (lost); the next report carries the whole state.
#
# The states are openbench-reports.md's ("The fixes"), not what OpenBench/nyayo.py does today.

import math

COUNTS = ('games', 'wins', 'losses', 'draws', 'LL', 'LD', 'DD', 'DW', 'WW')

# Scenarios that fail today, with the plan item that fixes each. The run passes while
# they fail and fails when one starts passing: remove the mark in the commit that fixes it.
XFAIL = {
    '4' : "openbench-reports.md, What's wrong 1: a deleted test stays running",
    '5' : "openbench-reports.md, What's wrong 1: its delete is reported running (fixed with 4)",
    '6' : "openbench-reports.md, What's wrong 2: a bench mismatch is reported stopped, not error",
    '7' : "openbench-reports.md, What's wrong 2: a stopped test that crashed is reported error",
}


class Event:

    def __init__(self, t, kind, test_id, before, after, report, state, lost):
        self.t, self.kind, self.test_id, self.lost = t, kind, test_id, lost
        self.before, self.after = before, after
        self.report, self.state = report, state

    def __repr__(self):
        return '%s(test %d -> %s)' % (self.kind, self.test_id, self.state if self.report else 'no report')


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
        expected = self.expected(test_id)
        match_id = 'openbench-%s-%d' % (self.instance, test_id)

        if len(reports) < len(expected):
            missing = expected[len(reports):]
            problems.append('missing %d report(s), for %s' % (len(missing), missing))
        if len(reports) > len(expected):
            problems.append('%d report(s) no event explains' % (len(reports) - len(expected)))

        for index, (event, report) in enumerate(zip(expected, reports)):
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

        return problems


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
