"""Email alerts for sync errors and documents held for a person.

Collected during a sync (or a single push) and sent as one email at the end.
The same problem on the same order is only emailed once a day.
"""

import hashlib
import logging

from django.core.cache import cache

logger = logging.getLogger('inventree')

DEDUPE_SECONDS = 24 * 60 * 60


def recipients(plugin):
    configured = [e.strip() for e in (plugin.get_setting('ALERT_EMAILS') or '').replace(';', ',').split(',') if e.strip()]
    if configured:
        return configured
    from django.contrib.auth import get_user_model

    return list(get_user_model().objects.filter(is_superuser=True, is_active=True)
                .exclude(email='').values_list('email', flat=True))


def _deliver(subject, body, to):
    from InvenTree.helpers_email import send_email

    return send_email(subject, body, to)


class Alerts:
    def __init__(self, plugin):
        self.plugin = plugin
        self.items = []

    def add(self, level, ref, message):
        key = 'inventree-xero-alert-' + hashlib.sha1(f'{ref}|{message}'.encode()).hexdigest()
        if not cache.add(key, '1', timeout=DEDUPE_SECONDS):
            return  # already told someone today
        self.items.append((level, ref, message))

    def send(self):
        if not self.items:
            return False
        to = recipients(self.plugin)
        if not to:
            logger.warning('Xero plugin: %d alert(s) but no recipients (set "Alert emails")', len(self.items))
            return False
        errors = [i for i in self.items if i[0] == 'error']
        held = [i for i in self.items if i[0] == 'held']
        subject = f'Xero sync: {len(errors)} error(s), {len(held)} held for review' if errors else \
            f'Xero sync: {len(held)} item(s) held for review'
        lines = ['The InvenTree ↔ Xero sync needs a person to look at the following.', '']
        if errors:
            lines.append('ERRORS')
            lines += [f'- {ref or "sync"}: {message}' for _, ref, message in errors]
            lines.append('')
        if held:
            lines.append('HELD (not sent to Xero until fixed)')
            lines += [f'- {ref}: {message}' for _, ref, message in held]
            lines.append('')
        lines.append('Each order\'s Xero panel in InvenTree shows the details; the full history is in the sync log.')
        try:
            ok, error = _deliver(subject, '\n'.join(lines), to)
        except Exception as exc:  # noqa: BLE001 — alerting must never break a sync
            logger.warning('Xero plugin: could not send alert email: %s', exc)
            return False
        if not ok:
            logger.warning('Xero plugin: alert email not sent: %s', error)
        self.items = []
        return bool(ok)
