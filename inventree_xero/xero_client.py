"""Xero Accounting API client.

Every write to Xero goes through one gate (`request` → `_write_gate`):

- **Paused** (the kill switch): nothing is sent; the write is refused.
- **Dry run**: nothing is sent; the intended write is recorded in the audit table and
  `DryRunWrite` is raised so the caller can report what *would* have happened.
- **Live**: the write is sent with an `Idempotency-Key`, so a retry after a network
  drop can never create a second document, and the request and Xero's reply are
  recorded in the audit table.

OAuth tokens are stored encrypted (crypto.py) and the OAuth scopes requested are
only those the enabled features need.
"""

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlencode

import requests
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('inventree')

AUTH_URL = 'https://login.xero.com/identity/connect/authorize'
TOKEN_URL = 'https://identity.xero.com/connect/token'
REVOKE_URL = 'https://identity.xero.com/connect/revocation'
CONNECTIONS_URL = 'https://api.xero.com/connections'
API_BASE = 'https://api.xero.com/api.xro/2.0'

TIMEOUT = 60
PAGE_SIZE = 100
WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')

# OAuth identity scopes are not valid for client_credentials (custom connections)
IDENTITY_SCOPES = {'openid', 'profile', 'email', 'offline_access'}

_DOTNET_DATE = re.compile(r'/Date\((-?\d+)([+-]\d{4})?\)/')

DOC_ENDPOINTS = {
    'po': ('PurchaseOrders', 'PurchaseOrders'),
    'invoice': ('Invoices', 'Invoices'),
    'bill': ('Invoices', 'Invoices'),
    'quote': ('Quotes', 'Quotes'),
}
DOC_ID_KEYS = {'po': 'PurchaseOrderID', 'invoice': 'InvoiceID', 'bill': 'InvoiceID', 'quote': 'QuoteID'}


class XeroError(Exception):
    """Raised for any Xero API / auth failure, carrying a readable message."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class DryRunWrite(XeroError):
    """Dry run: this write was recorded but not sent."""


class WritesPaused(XeroError):
    """The kill switch is on: no writes to Xero."""


def parse_xero_date(value):
    """Parse Xero's '/Date(ms+0000)/' or ISO strings into an aware datetime."""
    if not value or not isinstance(value, str):
        return None
    match = _DOTNET_DATE.match(value)
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=dt_timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def xero_error_message(payload):
    """Pull the human-readable validation messages out of a Xero error body."""
    if not isinstance(payload, dict):
        return str(payload)[:500]
    messages = []
    for element in payload.get('Elements') or []:
        for err in element.get('ValidationErrors') or []:
            if err.get('Message'):
                messages.append(err['Message'])
    for key in ('Message', 'Detail', 'Title', 'error_description', 'error'):
        if not messages and payload.get(key):
            messages.append(str(payload[key]))
    return '; '.join(messages) or json.dumps(payload)[:500]


def required_scopes(setting):
    """The least OAuth scopes for the features switched on (or the SCOPES setting, if filled in)."""
    custom = (setting('SCOPES') or '').strip()
    if custom:
        return custom.split()
    scopes = ['offline_access', 'accounting.invoices', 'accounting.settings.read']
    scopes.append('accounting.contacts.read' if (setting('CONTACT_SYNC') or 'create') == 'link'
                  else 'accounting.contacts')
    from InvenTree.helpers import str2bool

    if (setting('SHOP_PAYMENT_ACCOUNT') or '').strip() and str2bool(setting('AUTO_APPROVE')):
        scopes.append('accounting.payments')
    return scopes


ID_KEYS = ('PurchaseOrderID', 'InvoiceID', 'QuoteID', 'ContactID', 'ItemID', 'PaymentID', 'CreditNoteID')


def is_create(body):
    """A write that makes a new record (no record ID in the document sent)."""
    if not isinstance(body, dict) or not body:
        return False
    docs = next(iter(body.values()))
    doc = docs[0] if isinstance(docs, list) and docs else docs
    return isinstance(doc, dict) and not any(doc.get(k) for k in ID_KEYS)


def idempotency_key(tenant, method, path, body, scope):
    """Xero returns the first result for a repeated key instead of applying the request again.

    Creates use a key made from the request and the day (scope), so a create whose reply
    was lost - even across sync runs - can never make a second document. Updates use a
    fresh scope per request (kept across that request's retries), because repeating an
    identical update later is legitimate (e.g. a quote Sent -> Draft -> Sent).
    """
    canonical = json.dumps(body or {}, sort_keys=True, default=str)
    return hashlib.sha256(f'{tenant}|{method}|{path}|{canonical}|{scope}'.encode()).hexdigest()


class XeroClient:
    """Wrapper around the Xero Accounting API used by the sync engine and views."""

    def __init__(self, plugin):
        from .models import XeroConnection

        self.plugin = plugin
        self.conn = XeroConnection.get_solo()
        self.conn.encrypt_legacy_tokens()
        self.http = requests.Session()
        self.audit_context = {}
        self.force_live = False  # only the Demo Company self-test sets this

    # ------------------------------------------------------------------ settings
    def setting(self, key):
        return self.plugin.get_setting(key)

    @property
    def auth_mode(self):
        return self.setting('AUTH_MODE') or 'oauth'

    @property
    def write_mode(self):
        return self.setting('WRITE_MODE') or 'dry_run'

    @property
    def dry_run(self):
        return self.write_mode == 'dry_run' and not self.force_live

    @property
    def paused(self):
        return self.write_mode == 'paused'

    def _basic_auth(self):
        client_id = (self.setting('CLIENT_ID') or '').strip()
        secret = (self.setting('CLIENT_SECRET') or '').strip()
        if not client_id or not secret:
            raise XeroError('Xero Client ID and Client Secret must be set in the plugin settings')
        token = base64.b64encode(f'{client_id}:{secret}'.encode()).decode()
        return {'Authorization': f'Basic {token}'}

    def scopes(self, for_client_credentials=False):
        scopes = required_scopes(self.setting)
        if for_client_credentials:
            scopes = [s for s in scopes if s not in IDENTITY_SCOPES]
        return ' '.join(scopes)

    def missing_scopes(self):
        """Scopes the enabled features need that the current connection wasn't granted."""
        if not self.conn.scopes:
            return []
        granted = set(self.conn.scopes.split())
        missing = []
        for scope in self.scopes(for_client_credentials=True).split():
            # a full scope covers its .read variant
            if scope not in granted and not (scope.endswith('.read') and scope[:-5] in granted):
                missing.append(scope)
        return missing

    # ------------------------------------------------------------------ OAuth
    def authorize_url(self, redirect_uri, state):
        params = {
            'response_type': 'code',
            'client_id': (self.setting('CLIENT_ID') or '').strip(),
            'redirect_uri': redirect_uri,
            'scope': self.scopes(),
            'state': state,
        }
        return f'{AUTH_URL}?{urlencode(params)}'

    def _token_request(self, data):
        response = self.http.post(TOKEN_URL, data=data, headers=self._basic_auth(), timeout=TIMEOUT)
        try:
            payload = response.json()
        except ValueError:
            payload = {'error': response.text[:300]}
        if response.status_code != 200:
            raise XeroError(f'Xero token request failed: {xero_error_message(payload)}', response.status_code, payload)
        return payload

    def _store_tokens(self, payload):
        self.conn.set_tokens(access_token=payload['access_token'], refresh_token=payload.get('refresh_token'))
        self.conn.expires_at = timezone.now() + timedelta(seconds=int(payload.get('expires_in', 1800)) - 60)
        if payload.get('scope'):
            self.conn.scopes = payload['scope']
        # update_fields: never clobber sync bookkeeping written by other workers
        self.conn.save(update_fields=['access_token', 'refresh_token', 'expires_at', 'scopes'])

    def exchange_code(self, code, redirect_uri, user=None):
        """Complete the OAuth authorisation-code flow."""
        payload = self._token_request({
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri,
        })
        self._store_tokens(payload)
        self.conn.connected_at = timezone.now()
        self.conn.connected_by = getattr(user, 'username', '') or ''
        self.conn.save(update_fields=['connected_at', 'connected_by'])
        self.select_tenant()
        self.load_organisation()

    def connect_custom(self, user=None):
        """Connect using a Xero Custom Connection (client_credentials grant)."""
        data = {'grant_type': 'client_credentials'}
        scopes = self.scopes(for_client_credentials=True)
        if scopes:
            data['scope'] = scopes
        payload = self._token_request(data)
        self._store_tokens(payload)
        self.conn.connected_at = timezone.now()
        self.conn.connected_by = getattr(user, 'username', '') or ''
        self.conn.save(update_fields=['connected_at', 'connected_by'])
        try:
            self.select_tenant()
        except XeroError as exc:
            logger.info('Xero custom connection: tenant lookup skipped (%s)', exc)
        self.load_organisation()

    def select_tenant(self):
        status, _, payload = self._http('GET', CONNECTIONS_URL, headers=self._auth_headers(tenant=False))
        if status != 200:
            raise XeroError(f'Could not list Xero organisations ({status})', status)
        tenants = [t for t in payload if t.get('tenantType', 'ORGANISATION') == 'ORGANISATION']
        self.conn.tenants = [
            {'id': t.get('id'), 'tenantId': t.get('tenantId'), 'tenantName': t.get('tenantName')} for t in tenants
        ]
        wanted = (self.setting('TENANT_ID') or '').strip()
        chosen = next((t for t in tenants if t.get('tenantId') == wanted), None) or (tenants[0] if tenants else None)
        if chosen:
            self.conn.tenant_id = chosen.get('tenantId') or ''
            self.conn.tenant_name = chosen.get('tenantName') or ''
        self.conn.save(update_fields=['tenants', 'tenant_id', 'tenant_name'])

    def load_organisation(self):
        data = self.get('Organisation')
        orgs = data.get('Organisations') or []
        if orgs:
            self.conn.short_code = orgs[0].get('ShortCode') or ''
            self.conn.tenant_name = orgs[0].get('Name') or self.conn.tenant_name
            self.conn.save(update_fields=['short_code', 'tenant_name'])
        return orgs[0] if orgs else {}

    def disconnect(self):
        """Revoke the refresh token / remove the tenant connection and clear tokens."""
        try:
            refresh = self.conn.refresh_token_value
            if self.auth_mode == 'oauth' and refresh:
                self.http.post(REVOKE_URL, data={'token': refresh}, headers=self._basic_auth(), timeout=TIMEOUT)
            elif self.conn.access_token:
                for tenant in self.conn.tenants or []:
                    if tenant.get('id') and tenant.get('tenantId') == self.conn.tenant_id:
                        self.http.delete(f"{CONNECTIONS_URL}/{tenant['id']}", headers=self._auth_headers(tenant=False),
                                         timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — disconnect must always clear local tokens
            logger.warning('Xero revoke failed: %s', exc)
        self.conn.clear_tokens()
        self.conn.import_marks = {}
        self.conn.save(update_fields=['import_marks'])

    def _refresh(self):
        from .models import XeroConnection

        with transaction.atomic():
            conn = XeroConnection.objects.select_for_update().get(pk=self.conn.pk)
            # Another worker may have refreshed while we waited for the row lock
            if conn.access_token and conn.expires_at and conn.expires_at > timezone.now():
                self.conn = conn
                return
            self.conn = conn
            if self.auth_mode == 'custom':
                data = {'grant_type': 'client_credentials'}
                scopes = self.scopes(for_client_credentials=True)
                if scopes:
                    data['scope'] = scopes
                payload = self._token_request(data)
            else:
                refresh = conn.refresh_token_value
                if not refresh:
                    raise XeroError('Not connected to Xero — use "Connect to Xero" on the plugin settings page')
                try:
                    payload = self._token_request({'grant_type': 'refresh_token', 'refresh_token': refresh})
                except XeroError as exc:
                    if exc.status == 400:
                        raise XeroError(
                            'Xero refresh token is no longer valid (expired after 60 days unused, or revoked). '
                            'Reconnect from the plugin settings page.', exc.status, exc.payload,
                        ) from exc
                    raise
            self._store_tokens(payload)

    def ensure_token(self):
        if not self.conn.access_token or not self.conn.expires_at or self.conn.expires_at <= timezone.now():
            self._refresh()

    def _auth_headers(self, tenant=True):
        headers = {'Authorization': f'Bearer {self.conn.access_token_value}', 'Accept': 'application/json'}
        if tenant and self.conn.tenant_id:
            headers['xero-tenant-id'] = self.conn.tenant_id
        return headers

    # ------------------------------------------------------------------ HTTP
    def _http(self, method, url, params=None, json_body=None, headers=None):
        """One HTTP call. Returns (status, headers, parsed body). Tests replace this with a fake Xero."""
        response = self.http.request(method, url, params=params, json=json_body, headers=headers, timeout=TIMEOUT)
        if response.status_code == 304:
            return 304, response.headers, {}
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {'Message': response.text[:500]}
        return response.status_code, response.headers, payload

    def _audit(self, mode, method, path, key, body, status=None, response=None, ok=False, error=''):
        from .models import XeroWriteAudit

        ctx = self.audit_context
        try:
            XeroWriteAudit.objects.create(
                mode=mode, method=method, path=path[:200], idempotency_key=key or '',
                doc_type=str(ctx.get('doc_type', ''))[:20], inventree_ref=str(ctx.get('ref', ''))[:100],
                inventree_pk=ctx.get('pk'), request_body=body or {},
                response_status=status, response_body=response if isinstance(response, dict) else {'raw': str(response)[:2000]},
                ok=ok, error=str(error)[:4000],
            )
        except Exception:  # noqa: BLE001 — auditing must never break a sync
            logger.exception('Xero: could not write audit record')

    def _write_gate(self, method, path, body):
        """Refuse or record writes according to the write mode. Returns the idempotency key for live writes."""
        if self.paused:
            raise WritesPaused('Writes to Xero are paused (kill switch in the plugin settings)')
        scope = timezone.localdate().isoformat() if is_create(body) else uuid.uuid4().hex
        key = idempotency_key(self.conn.tenant_id, method, path, body, scope)
        if self.dry_run:
            self._audit('dry_run', method, path, key, body, ok=True)
            raise DryRunWrite(f'Dry run: would {method} {path}')
        return key

    def request(self, method, path, params=None, json_body=None, modified_since=None):
        is_write = method in WRITE_METHODS
        key = self._write_gate(method, path, json_body) if is_write else None
        self.ensure_token()
        url = f"{API_BASE}/{path.lstrip('/')}"
        auth_retried = False
        attempt = 0
        while True:
            headers = self._auth_headers()
            if key:
                headers['Idempotency-Key'] = key
            if modified_since:
                headers['If-Modified-Since'] = modified_since.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
            try:
                status, resp_headers, payload = self._http(method, url, params=params, json_body=json_body,
                                                           headers=headers)
            except requests.RequestException as exc:
                # Safe to retry writes too: the same Idempotency-Key means Xero won't apply them twice
                if attempt < 3:
                    attempt += 1
                    time.sleep(2 * attempt)
                    continue
                if is_write:
                    self._audit('live', method, path, key, json_body, error=f'network: {exc}')
                raise XeroError(f'Network error talking to Xero: {exc}') from exc

            if status == 401 and not auth_retried:
                auth_retried = True
                self.conn.expires_at = None
                self._refresh()
                continue
            if status == 429 and attempt < 5:
                wait = int((resp_headers or {}).get('Retry-After', '5') or 5)
                if wait > 120:
                    raise XeroError(
                        f"Xero rate limit reached ({(resp_headers or {}).get('X-Rate-Limit-Problem', 'limit')}); "
                        f'retry after {wait}s', 429,
                    )
                attempt += 1
                time.sleep(wait + 1)
                continue
            if status >= 500 and attempt < 3:
                attempt += 1
                time.sleep(2 ** attempt)
                continue
            break

        if is_write:
            self._audit('live', method, path, key, json_body, status=status, response=payload,
                        ok=status < 400, error='' if status < 400 else xero_error_message(payload))
        if status == 304:
            return {}
        if status >= 400:
            raise XeroError(xero_error_message(payload), status, payload)
        return payload

    def get(self, path, params=None, modified_since=None):
        return self.request('GET', path, params=params, modified_since=modified_since)

    def get_all(self, path, key, params=None, modified_since=None, max_pages=200):
        """Fetch every page of a paginated list endpoint."""
        results = []
        for page in range(1, max_pages + 1):
            page_params = dict(params or {})
            page_params['page'] = page
            data = self.get(path, params=page_params, modified_since=modified_since)
            rows = data.get(key) or []
            results.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
        return results

    def post_document(self, path, key, document):
        """Create-or-update a single document (POST with its ID updates it)."""
        data = self.request('POST', path, params={'unitdp': 4, 'summarizeErrors': 'false'}, json_body={key: [document]})
        rows = data.get(key) or []
        if not rows:
            raise XeroError(f'Xero returned no {key} in response')
        row = rows[0]
        errors = [e.get('Message') for e in (row.get('ValidationErrors') or []) if e.get('Message')]
        if row.get('HasErrors') or errors:
            raise XeroError('; '.join(errors) or f'Xero rejected the {key[:-1]}', 400, row)
        return row

    # ------------------------------------------------------------------ resources
    def get_document(self, doc_type, xero_id):
        path, key = DOC_ENDPOINTS[doc_type]
        rows = self.get(f'{path}/{xero_id}').get(key) or []
        if not rows:
            raise XeroError(f'{doc_type} {xero_id} not found in Xero', 404)
        return rows[0]

    def search_contacts(self, term):
        return self.get('Contacts', params={'searchTerm': term, 'page': 1}).get('Contacts') or []

    def create_contact(self, payload):
        return self.post_document('Contacts', 'Contacts', payload)

    def get_contacts_by_id(self, contact_ids):
        """Full contact records (with Balances) for up to ~50 ContactIDs per call."""
        if not contact_ids:
            return []
        return self.get('Contacts', params={'IDs': ','.join(contact_ids), 'page': 1}).get('Contacts') or []

    def get_contact(self, contact_id):
        rows = self.get(f'Contacts/{contact_id}').get('Contacts') or []
        return rows[0] if rows else {}

    def get_items(self):
        return self.get('Items').get('Items') or []

    def create_item(self, payload):
        return self.post_document('Items', 'Items', payload)

    def create_payment(self, payload):
        """Apply a payment to an invoice (needs the accounting.payments scope)."""
        data = self.request('PUT', 'Payments', params={'summarizeErrors': 'false'}, json_body={'Payments': [payload]})
        rows = data.get('Payments') or []
        if not rows:
            raise XeroError('Xero returned no payment in response')
        errors = [e.get('Message') for e in (rows[0].get('ValidationErrors') or []) if e.get('Message')]
        if errors:
            raise XeroError('; '.join(errors), 400, rows[0])
        return rows[0]
