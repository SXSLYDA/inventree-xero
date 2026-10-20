"""python manage.py xero_selftest --confirm-demo - live self-test against a Xero Demo Company."""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Live self-test of the Xero plugin against a connected Xero Demo Company (writes tagged test documents)'

    def add_arguments(self, parser):
        parser.add_argument('--confirm-demo', action='store_true',
                            help='Confirm that test documents may be written to the connected Demo Company')

    def handle(self, *args, **options):
        from plugin.registry import registry

        from inventree_xero.selftest import run_selftest

        plugin = registry.get_plugin('xero')
        if plugin is None:
            self.stderr.write('The Xero plugin is not active')
            return
        report = run_selftest(plugin, confirm_demo=options['confirm_demo'])
        self.stdout.write(report.as_text())
        if not report.passed:
            raise SystemExit(1)
