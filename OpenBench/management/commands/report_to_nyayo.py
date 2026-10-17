from django.core.management.base import BaseCommand, CommandError

from OpenBench.models import Test
from OpenBench.nyayo import nyayo_enabled, report_test


class Command(BaseCommand):

    help = ('Sends tests to nyayo now, as the reporter does after each batch: to backfill, and to resend after an '
            'outage or a fix. Needs NYAYO_URL, NYAYO_TOKEN and NYAYO_INSTANCE.')

    def add_arguments(self, parser):
        parser.add_argument('test_ids', nargs='*', type=int, help='the tests to send')
        parser.add_argument('--all', action='store_true', help='every test, oldest first')

    def handle(self, *args, **options):
        if not nyayo_enabled():
            raise CommandError('nyayo is not configured: set NYAYO_URL, NYAYO_TOKEN and NYAYO_INSTANCE')
        if options['all'] == bool(options['test_ids']):
            raise CommandError('give test ids or --all, not both or neither')

        if options['all']:
            ids = list(Test.objects.order_by('id').values_list('id', flat=True))
        else:
            ids = options['test_ids']
            unknown = sorted(set(ids) - set(Test.objects.filter(id__in=ids).values_list('id', flat=True)))
            if unknown:
                raise CommandError('no such test: %s' % (', '.join(map(str, unknown))))

        failed = [test_id for test_id in ids if not report_test(test_id)]
        for test_id in ids:
            self.stdout.write('%s test %d' % ('failed' if test_id in failed else 'sent', test_id))

        if failed:
            raise CommandError('%d of %d test(s) failed, see the log above' % (len(failed), len(ids)))
