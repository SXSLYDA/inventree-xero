"""python manage.py xero_check - read-only configuration check (safe on your real Xero organisation)."""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Check the Xero plugin configuration against the connected Xero organisation (reads only)'

    def handle(self, *args, **options):
        from plugin.registry import registry

        from inventree_xero.selftest import run_check

        plugin = registry.get_plugin('xero')
        if plugin is None:
            self.stderr.write('The Xero plugin is not active')
            return
        report = run_check(plugin)
        self.stdout.write(report.as_text())
        if not report.passed:
            raise SystemExit(1)
