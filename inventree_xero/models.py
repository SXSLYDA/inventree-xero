"""Database models for the Xero plugin.

Order / company / part links to Xero are stored in each object's InvenTree
``metadata['xero']`` so they travel with the object and need no join tables.
These models only hold the connection (OAuth tokens, which are too long for a
plugin setting) and the sync activity log.
"""

from django.db import models
from django.utils import timezone


class XeroConnection(models.Model):
    """Singleton row holding the Xero OAuth connection and sync bookkeeping."""

    tenant_id = models.CharField(max_length=64, blank=True)
    tenant_name = models.CharField(max_length=200, blank=True)
    short_code = models.CharField(max_length=20, blank=True)
    tenants = models.JSONField(default=list, blank=True)

    access_token = models.TextField(blank=True)
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True)

    connected_at = models.DateTimeField(null=True, blank=True)
    connected_by = models.CharField(max_length=150, blank=True)

    last_sync_started = models.DateTimeField(null=True, blank=True)
    last_sync_finished = models.DateTimeField(null=True, blank=True)
    last_sync_ok = models.BooleanField(null=True, blank=True)
    last_sync_message = models.TextField(blank=True)

    # Per-document-type high-water marks for incremental imports (ISO strings)
    import_marks = models.JSONField(default=dict, blank=True)

    class Meta:
        app_label = 'inventree_xero'
        verbose_name = 'Xero connection'
        verbose_name_plural = 'Xero connection'

    def __str__(self):
        return self.tenant_name or 'Xero (not connected)'

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_connected(self):
        return bool(self.access_token and (self.refresh_token or self.tenant_id or self.expires_at))

    # Tokens are stored encrypted (see crypto.py); always read/write them through these.
    @property
    def access_token_value(self):
        from .crypto import decrypt

        return decrypt(self.access_token)

    @property
    def refresh_token_value(self):
        from .crypto import decrypt

        return decrypt(self.refresh_token)

    def set_tokens(self, access_token=None, refresh_token=None):
        from .crypto import encrypt

        if access_token is not None:
            self.access_token = encrypt(access_token)
        if refresh_token is not None:
            self.refresh_token = encrypt(refresh_token)

    def encrypt_legacy_tokens(self):
        """Encrypt tokens saved in plain text by versions before 1.3."""
        from .crypto import encrypt, is_encrypted

        changed = []
        for field in ('access_token', 'refresh_token'):
            value = getattr(self, field)
            if value and not is_encrypted(value):
                setattr(self, field, encrypt(value))
                changed.append(field)
        if changed:
            self.save(update_fields=changed)

    def clear_tokens(self):
        self.access_token = ''
        self.refresh_token = ''
        self.expires_at = None
        self.tenant_id = ''
        self.tenant_name = ''
        self.short_code = ''
        self.tenants = []
        self.scopes = ''
        self.connected_at = None
        self.connected_by = ''
        self.save()


class XeroSyncLog(models.Model):
    """One entry per sync action, shown in the plugin panels and Django admin."""

    LEVELS = [('info', 'Info'), ('warning', 'Warning'), ('error', 'Error')]
    DIRECTIONS = [('export', 'InvenTree → Xero'), ('import', 'Xero → InvenTree'), ('system', 'System')]

    created = models.DateTimeField(default=timezone.now, db_index=True)
    level = models.CharField(max_length=10, choices=LEVELS, default='info')
    direction = models.CharField(max_length=10, choices=DIRECTIONS, default='system')
    doc_type = models.CharField(max_length=20, blank=True, help_text='po, so, contact, item, system')
    action = models.CharField(max_length=20, blank=True)
    inventree_ref = models.CharField(max_length=100, blank=True)
    inventree_pk = models.IntegerField(null=True, blank=True)
    xero_id = models.CharField(max_length=64, blank=True)
    xero_number = models.CharField(max_length=100, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        app_label = 'inventree_xero'
        ordering = ['-created']
        verbose_name = 'Xero sync log entry'
        verbose_name_plural = 'Xero sync log'

    def __str__(self):
        return f'{self.created:%Y-%m-%d %H:%M} {self.level} {self.doc_type} {self.inventree_ref or self.xero_number}'

    def to_dict(self):
        return {
            'id': self.pk,
            'created': self.created.isoformat(),
            'level': self.level,
            'direction': self.direction,
            'doc_type': self.doc_type,
            'action': self.action,
            'inventree_ref': self.inventree_ref,
            'inventree_pk': self.inventree_pk,
            'xero_id': self.xero_id,
            'xero_number': self.xero_number,
            'message': self.message,
        }


class XeroQuoteLink(models.Model):
    """A web shop quote request (inventree-woocommerce QuoteRequest) and its Xero quote.

    Stored here rather than on the QuoteRequest so the shop plugin needs no changes.
    """

    quote_request_id = models.PositiveIntegerField(unique=True)
    reference = models.CharField(max_length=32, blank=True, db_index=True)
    xero_id = models.CharField(max_length=64, blank=True, db_index=True)
    number = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, blank=True, help_text='Xero quote status')
    shop_status = models.CharField(max_length=20, blank=True, help_text='Shop quote status when last synced')
    url = models.CharField(max_length=500, blank=True)
    hash = models.CharField(max_length=40, blank=True, help_text='What was last sent to Xero')
    shop_sig = models.CharField(max_length=40, blank=True, help_text='Shop quote lines when last synced')
    xero_sig = models.CharField(max_length=40, blank=True, help_text='Xero quote lines when last synced')
    total = models.CharField(max_length=40, blank=True)
    updated = models.DateTimeField(null=True, blank=True, help_text='Xero UpdatedDateUTC when last synced')
    synced = models.DateTimeField(null=True, blank=True)
    sales_order_id = models.PositiveIntegerField(null=True, blank=True)
    invoiced = models.BooleanField(default=False)
    final = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = 'inventree_xero'
        ordering = ['-created']
        verbose_name = 'Xero quote link'
        verbose_name_plural = 'Xero quote links'

    def __str__(self):
        return f'{self.reference} ↔ {self.number or "(not in Xero)"}'

    def to_dict(self):
        return {
            'id': self.pk,
            'quote_request_id': self.quote_request_id,
            'reference': self.reference,
            'xero_id': self.xero_id,
            'number': self.number,
            'status': self.status,
            'shop_status': self.shop_status,
            'url': self.url,
            'total': self.total,
            'synced': self.synced.isoformat() if self.synced else None,
            'sales_order_id': self.sales_order_id,
            'invoiced': self.invoiced,
            'final': self.final,
            'error': self.error,
        }


class XeroWriteAudit(models.Model):
    """Every write to Xero (and every write a dry run would have made): what was sent and what Xero said."""

    MODES = [('live', 'Live'), ('dry_run', 'Dry run')]

    created = models.DateTimeField(default=timezone.now, db_index=True)
    mode = models.CharField(max_length=10, choices=MODES, default='live')
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=200)
    doc_type = models.CharField(max_length=20, blank=True)
    inventree_ref = models.CharField(max_length=100, blank=True, db_index=True)
    inventree_pk = models.IntegerField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128, blank=True)
    request_body = models.JSONField(default=dict, blank=True)
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(default=dict, blank=True)
    ok = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    class Meta:
        app_label = 'inventree_xero'
        ordering = ['-created']
        verbose_name = 'Xero write (audit)'
        verbose_name_plural = 'Xero writes (audit)'

    def __str__(self):
        return f'{self.created:%Y-%m-%d %H:%M} {self.mode} {self.method} {self.path} {self.inventree_ref}'

    def to_dict(self):
        return {
            'id': self.pk,
            'created': self.created.isoformat(),
            'mode': self.mode,
            'method': self.method,
            'path': self.path,
            'doc_type': self.doc_type,
            'inventree_ref': self.inventree_ref,
            'response_status': self.response_status,
            'ok': self.ok,
            'error': self.error,
        }
