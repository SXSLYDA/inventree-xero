"""OAuth views and JSON API for the Xero plugin UI (served under /plugin/xero/)."""

import logging
import secrets
from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.html import escape
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .mapping import META_KEY, xmeta
from .models import XeroConnection, XeroSyncLog
from .xero_client import XeroClient, XeroError

logger = logging.getLogger('inventree')

SESSION_STATE_KEY = 'xero_oauth_state'
ORDER_URLS = {'po': '/web/purchasing/purchase-order/{pk}', 'so': '/web/sales/sales-order/{pk}'}
ORDER_PERMS = {
    'po': ('order.view_purchaseorder', 'order.change_purchaseorder'),
    'so': ('order.view_salesorder', 'order.change_salesorder'),
}


def get_plugin():
    from plugin.registry import registry

    plugin = registry.get_plugin('xero')
    if plugin is None:
        raise XeroError('Xero plugin is not active')
    return plugin


def redirect_uri(request, plugin):
    base = (plugin.get_setting('BASE_URL') or '').strip().rstrip('/')
    if not base:
        base = request.build_absolute_uri('/').rstrip('/')
    return f'{base}{plugin.base_url}oauth/callback/'


def connection_info(plugin, request=None):
    conn = XeroConnection.get_solo()
    return {
        'connected': conn.is_connected,
        'auth_mode': plugin.get_setting('AUTH_MODE'),
        'tenant_id': conn.tenant_id,
        'tenant_name': conn.tenant_name,
        'tenants': conn.tenants,
        'scopes': conn.scopes,
        'connected_at': conn.connected_at.isoformat() if conn.connected_at else None,
        'connected_by': conn.connected_by,
        'token_expires': conn.expires_at.isoformat() if conn.expires_at else None,
        'last_sync_started': conn.last_sync_started.isoformat() if conn.last_sync_started else None,
        'last_sync_finished': conn.last_sync_finished.isoformat() if conn.last_sync_finished else None,
        'last_sync_ok': conn.last_sync_ok,
        'last_sync_message': conn.last_sync_message,
        'scheduled': bool(plugin.get_setting('ENABLE_SCHEDULED')),
        'interval_minutes': plugin.get_setting('SYNC_INTERVAL_MINUTES'),
        'redirect_uri': redirect_uri(request, plugin) if request else '',
        'connect_url': f'{plugin.base_url}oauth/connect/',
        'po_direction': plugin.get_setting('PO_SYNC_DIRECTION'),
        'so_direction': plugin.get_setting('SO_SYNC_DIRECTION'),
        'so_document': plugin.get_setting('SO_XERO_DOCUMENT'),
        **safety_info(plugin),
    }


def safety_info(plugin):
    from InvenTree.helpers import str2bool
    from order.models import PurchaseOrder, SalesOrder

    from .models import XeroQuoteLink
    from .xero_client import XeroClient, required_scopes

    held = (PurchaseOrder.objects.filter(Q(**{f'metadata__{META_KEY}__has_key': 'held'})
                                         | Q(**{f'metadata__{META_KEY}__bill__has_key': 'held'})).count()
            + SalesOrder.objects.filter(**{f'metadata__{META_KEY}__has_key': 'held'}).count()
            + XeroQuoteLink.objects.exclude(error='').filter(final=False).count())
    try:
        missing = XeroClient(plugin).missing_scopes()
    except Exception:  # noqa: BLE001 — status must always load
        missing = []
    return {
        'write_mode': plugin.get_setting('WRITE_MODE') or 'dry_run',
        'auto_approve': str2bool(plugin.get_setting('AUTO_APPROVE')),
        'allow_delete_void': str2bool(plugin.get_setting('ALLOW_DELETE_VOID')),
        'import_new_documents': str2bool(plugin.get_setting('IMPORT_NEW_DOCUMENTS')),
        'held': held,
        'required_scopes': required_scopes(plugin.get_setting),
        'missing_scopes': missing,
    }


def _page(title, body, ok=True):
    colour = '#2b8a3e' if ok else '#c92a2a'
    html = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f'<title>{escape(title)}</title></head>'
        '<body style="font-family:system-ui,sans-serif;max-width:560px;margin:60px auto;padding:0 16px">'
        f'<h2 style="color:{colour}">{escape(title)}</h2><p>{body}</p>'
        '<p><a href="/web/settings/admin/plugin">Back to InvenTree plugin settings</a></p></body></html>'
    )
    return HttpResponse(html, status=200 if ok else 400)


# ---------------------------------------------------------------------- OAuth
@staff_member_required
def oauth_connect(request):
    plugin = get_plugin()
    if plugin.get_setting('AUTH_MODE') == 'custom':
        return _page('Custom Connection mode', 'Use the "Connect" button on the plugin settings page instead.', ok=False)
    if not plugin.get_setting('CLIENT_ID') or not plugin.get_setting('CLIENT_SECRET'):
        return _page('Xero app not configured', 'Enter the Client ID and Client secret in the plugin settings first.', ok=False)
    state = secrets.token_urlsafe(32)
    request.session[SESSION_STATE_KEY] = state
    return redirect(XeroClient(plugin).authorize_url(redirect_uri(request, plugin), state))


@staff_member_required
def oauth_callback(request):
    plugin = get_plugin()
    if request.GET.get('error'):
        return _page('Xero connection cancelled', escape(request.GET.get('error_description') or request.GET['error']), ok=False)
    expected = request.session.pop(SESSION_STATE_KEY, None)
    if not expected or request.GET.get('state') != expected:
        return HttpResponseBadRequest('Invalid OAuth state — start the connection again from InvenTree.')
    code = request.GET.get('code')
    if not code:
        return HttpResponseBadRequest('Missing authorisation code')
    try:
        client = XeroClient(plugin)
        client.exchange_code(code, redirect_uri(request, plugin), user=request.user)
    except XeroError as exc:
        logger.warning('Xero OAuth callback failed: %s', exc)
        return _page('Xero connection failed', escape(str(exc)), ok=False)
    conn = client.conn
    XeroSyncLog.objects.create(level='info', direction='system', doc_type='system', action='connect',
                               message=f'Connected to Xero organisation {conn.tenant_name} by {request.user}')
    return _page('Connected to Xero', f'InvenTree is now linked to <b>{escape(conn.tenant_name)}</b>. '
                                      'The scheduled sync will start on its next run.')


# ---------------------------------------------------------------------- API base
class StaffPermission(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)


class StaffForWrites(permissions.BasePermission):
    """Anyone signed in can look; only staff can trigger anything that reaches Xero or changes a link."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class XeroAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated, StaffForWrites]

    def handle_exception(self, exc):
        if isinstance(exc, XeroError):
            return Response({'error': str(exc)}, status=400)
        return super().handle_exception(exc)


class StatusView(XeroAPIView):
    def get(self, request):
        plugin = get_plugin()
        data = connection_info(plugin, request)
        data['recent'] = [log.to_dict() for log in XeroSyncLog.objects.all()[:10]]
        data['errors_24h'] = XeroSyncLog.objects.filter(
            level='error', created__gte=timezone.now() - timedelta(hours=24)
        ).count()
        return Response(data)


class ConnectCustomView(XeroAPIView):
    permission_classes = [StaffPermission]

    def post(self, request):
        plugin = get_plugin()
        client = XeroClient(plugin)
        client.connect_custom(user=request.user)
        XeroSyncLog.objects.create(level='info', direction='system', doc_type='system', action='connect',
                                   message=f'Custom connection to {client.conn.tenant_name} by {request.user}')
        return Response(connection_info(plugin, request))


class DisconnectView(XeroAPIView):
    permission_classes = [StaffPermission]

    def post(self, request):
        plugin = get_plugin()
        XeroClient(plugin).disconnect()
        XeroSyncLog.objects.create(level='warning', direction='system', doc_type='system', action='disconnect',
                                   message=f'Disconnected from Xero by {request.user}')
        return Response(connection_info(plugin, request))


class TestConnectionView(XeroAPIView):
    permission_classes = [StaffPermission]

    def post(self, request):
        plugin = get_plugin()
        org = XeroClient(plugin).load_organisation()
        return Response({
            'ok': True,
            'organisation': org.get('Name'),
            'base_currency': org.get('BaseCurrency'),
            'country': org.get('CountryCode'),
            'short_code': org.get('ShortCode'),
        })


class SyncNowView(XeroAPIView):
    permission_classes = [StaffPermission]

    def post(self, request):
        from InvenTree.tasks import offload_task

        direction = (request.data or {}).get('direction', 'all')
        if direction not in ('all', 'export', 'import'):
            return Response({'error': 'direction must be all, export or import'}, status=400)
        queued = offload_task('plugin.registry.call_plugin_function', 'xero', 'run_manual_sync', direction,
                              force_async=True)
        if not queued:
            return Response({'error': 'Could not queue the sync — is the background worker running?'}, status=503)
        return Response({'queued': True, 'direction': direction})


class LogView(XeroAPIView):
    def get(self, request):
        qs = XeroSyncLog.objects.all()
        level = request.query_params.get('level')
        if level:
            qs = qs.filter(level=level)
        doc_type = request.query_params.get('doc_type')
        if doc_type:
            qs = qs.filter(doc_type=doc_type)
        q = request.query_params.get('q')
        if q:
            qs = qs.filter(Q(message__icontains=q) | Q(inventree_ref__icontains=q) | Q(xero_number__icontains=q))
        try:
            limit = min(int(request.query_params.get('limit', 50)), 500)
        except ValueError:
            limit = 50
        return Response([log.to_dict() for log in qs[:limit]])


# ---------------------------------------------------------------------- search
def _status_label(order):
    try:
        return order.get_status_display()
    except (AttributeError, ValueError):
        return str(order.status)


def _link_summary(meta):
    return {
        'linked': bool(meta.get('id')),
        'id': meta.get('id'),
        'doc_type': meta.get('doc_type'),
        'number': meta.get('number'),
        'status': meta.get('status'),
        'url': meta.get('url'),
        'total': meta.get('total'),
        'amount_due': meta.get('amount_due'),
        'amount_paid': meta.get('amount_paid'),
        'synced': meta.get('synced'),
        'origin': meta.get('origin'),
        'error': meta.get('error'),
        'xero_only': meta.get('xero_only'),
        'held': (meta.get('held') or {}).get('reason'),
        'dry_run': (meta.get('dry_run') or {}).get('would'),
    }


def _order_row(order, kind):
    meta = xmeta(order)
    party = order.supplier if kind == 'po' else order.customer
    total = getattr(order, 'total_price', None)
    woo = (order.metadata or {}).get('woocommerce') if kind == 'so' else None
    row = {
        'kind': kind,
        'pk': order.pk,
        'reference': order.reference,
        'party': party.name if party else '',
        'party_reference': order.supplier_reference if kind == 'po' else order.customer_reference,
        'description': order.description,
        'status': _status_label(order),
        'creation_date': order.creation_date.isoformat() if order.creation_date else None,
        'target_date': order.target_date.isoformat() if order.target_date else None,
        'total': str(total) if total is not None else None,
        'url': ORDER_URLS[kind].format(pk=order.pk),
        'xero': _link_summary(meta),
    }
    if kind == 'po':
        row['bill'] = _link_summary(meta.get('bill') or {})
    if kind == 'so':
        from .models import XeroQuoteLink

        quote = XeroQuoteLink.objects.filter(sales_order_id=order.pk).first()
        if quote:
            row['quote'] = {'reference': quote.reference, 'number': quote.number, 'status': quote.status,
                            'url': quote.url}
    if woo:
        row['shop'] = {
            'number': woo.get('number'),
            'on_account': bool(woo.get('on_account')),
            'payment_recorded': bool(meta.get('payment_recorded')),
        }
    return row


class SearchView(XeroAPIView):
    """Search InvenTree POs/SOs by reference, party, party ref, description, Xero number or Xero status.

    Query params: q, type (all|po|so), linked (any|yes|no|error), xero_status, limit
    """

    def get(self, request):
        from order.models import PurchaseOrder, SalesOrder

        params = request.query_params
        q = (params.get('q') or '').strip()
        kind = params.get('type', 'all')
        linked = params.get('linked', 'any')
        xero_status = (params.get('xero_status') or '').strip().upper()
        try:
            limit = min(int(params.get('limit', 50)), 250)
        except ValueError:
            limit = 50

        results = []
        sources = []
        if kind in ('all', 'po') and request.user.has_perm(ORDER_PERMS['po'][0]):
            sources.append(('po', PurchaseOrder.objects.select_related('supplier'), 'supplier'))
        if kind in ('all', 'so') and request.user.has_perm(ORDER_PERMS['so'][0]):
            sources.append(('so', SalesOrder.objects.select_related('customer'), 'customer'))

        for code, qs, party in sources:
            party_ref = 'supplier_reference' if code == 'po' else 'customer_reference'
            if q:
                qs = qs.filter(
                    Q(reference__icontains=q)
                    | Q(**{f'{party}__name__icontains': q})
                    | Q(**{f'{party_ref}__icontains': q})
                    | Q(description__icontains=q)
                    | Q(**{f'metadata__{META_KEY}__number__icontains': q})
                    | Q(**{f'metadata__{META_KEY}__reference__icontains': q})
                    | Q(**{f'metadata__{META_KEY}__bill__number__icontains': q})
                    | Q(**{f'metadata__{META_KEY}__bill__reference__icontains': q})
                    | Q(metadata__woocommerce__number__icontains=q)
                )
            if linked == 'yes':
                qs = qs.filter(**{f'metadata__{META_KEY}__has_key': 'id'})
            elif linked == 'no':
                qs = qs.exclude(**{f'metadata__{META_KEY}__has_key': 'id'})
            elif linked == 'held':
                qs = qs.filter(Q(**{f'metadata__{META_KEY}__has_key': 'held'})
                               | Q(**{f'metadata__{META_KEY}__bill__has_key': 'held'}))
            elif linked == 'error':
                qs = qs.filter(**{f'metadata__{META_KEY}__has_key': 'error'}).exclude(
                    **{f'metadata__{META_KEY}__error': None})
            if xero_status:
                qs = qs.filter(Q(**{f'metadata__{META_KEY}__status': xero_status})
                               | Q(**{f'metadata__{META_KEY}__bill__status': xero_status}))
            if params.get('source') == 'shop':
                qs = qs.filter(metadata__has_key='woocommerce')
            for order in qs.order_by('-pk')[:limit]:
                results.append(_order_row(order, code))

        results.sort(key=lambda r: (r['creation_date'] or '', r['pk']), reverse=True)
        return Response({'count': len(results), 'results': results[:limit]})


# ---------------------------------------------------------------------- per-object
class OrderXeroView(XeroAPIView):
    """GET → Xero info for an order. POST .../push/ or .../pull/ → sync that order now."""

    def _order(self, kind, pk):
        from order.models import PurchaseOrder, SalesOrder

        if kind not in ('po', 'so'):
            raise XeroError('kind must be po or so')
        model = PurchaseOrder if kind == 'po' else SalesOrder
        return model.objects.get(pk=pk)

    def get(self, request, kind, pk, action=None):
        if kind not in ORDER_PERMS or not request.user.has_perm(ORDER_PERMS[kind][0]):
            return Response({'error': 'Permission denied'}, status=403)
        if action in ('preview', 'preview-bill'):
            from .sync import XeroSync

            sync = XeroSync(get_plugin())
            sync.client.ensure_token()
            return Response(sync.preview_single('bill' if action == 'preview-bill' else kind, pk))
        order = self._order(kind, pk)
        row = _order_row(order, kind)
        doc_types = ['po', 'bill'] if kind == 'po' else ['so']
        row['log'] = [log.to_dict() for log in XeroSyncLog.objects.filter(
            doc_type__in=doc_types, inventree_pk=order.pk)[:12]]
        row['connected'] = XeroConnection.get_solo().is_connected
        plugin = get_plugin()
        row['bills_mode'] = plugin.get_setting('BILLS_MODE')
        row['write_mode'] = plugin.get_setting('WRITE_MODE') or 'dry_run'
        return Response(row)

    def post(self, request, kind, pk, action=None):
        from .mapping import write_meta
        from .sync import XeroSync

        if kind not in ORDER_PERMS or not request.user.has_perm(ORDER_PERMS[kind][1]):
            return Response({'error': 'Permission denied'}, status=403)
        sync = XeroSync(get_plugin())
        order = self._order(kind, pk)
        if action == 'push':
            ok, message = sync.export_single(kind, pk, force=True)
        elif action == 'bill' and kind == 'po':
            ok, message = sync.export_single('bill', pk, force=True)
        elif action == 'pull':
            ok, message = sync.pull_single(kind, pk)
        elif action == 'unlink':
            meta = xmeta(order)
            keep = {k: meta[k] for k in ('bill',) if k in meta}
            write_meta(order, keep)
            ok, message = True, 'Xero link removed (the Xero document was not changed)'
        elif action == 'unlink-bill' and kind == 'po':
            meta = xmeta(order)
            meta.pop('bill', None)
            write_meta(order, meta)
            ok, message = True, 'Bill link removed (the Xero bill was not changed)'
        else:
            return Response({'error': 'Unknown action'}, status=400)
        row = _order_row(self._order(kind, pk), kind)
        row.update({'ok': ok, 'message': message})
        return Response(row, status=200 if ok else 400)


class CompanyXeroView(XeroAPIView):
    """GET → Xero contact link and balances for a company. POST → link / create / push / unlink."""

    def _payload(self, company):
        meta = xmeta(company)
        return {
            'pk': company.pk,
            'name': company.name,
            'is_supplier': company.is_supplier,
            'is_customer': company.is_customer,
            'xero': {
                'linked': bool(meta.get('contact_id')),
                'contact_id': meta.get('contact_id'),
                'contact_name': meta.get('contact_name'),
                'url': meta.get('url'),
                'synced': meta.get('synced'),
                'balance': meta.get('balance'),
            },
            'connected': XeroConnection.get_solo().is_connected,
        }

    def get(self, request, pk):
        from company.models import Company

        if not request.user.has_perm('company.view_company'):
            return Response({'error': 'Permission denied'}, status=403)
        return Response(self._payload(Company.objects.get(pk=pk)))

    def post(self, request, pk):
        from company.models import Company

        from .mapping import write_meta
        from .sync import XeroSync

        if not request.user.has_perm('company.change_company'):
            return Response({'error': 'Permission denied'}, status=403)
        company = Company.objects.get(pk=pk)
        action = (request.data or {}).get('action', 'link')
        if action == 'unlink':
            meta = xmeta(company)
            for key in ('contact_id', 'contact_name', 'url', 'synced', 'contact_hash', 'balance'):
                meta.pop(key, None)
            write_meta(company, meta)
        else:
            sync = XeroSync(get_plugin())
            sync.client.ensure_token()
            if action == 'link_id':
                contact = sync.client.get_contact((request.data or {}).get('contact_id', ''))
                if not contact.get('ContactID'):
                    raise XeroError('That Xero contact was not found')
                sync.contacts.link(company, contact)
                sync.log('info', 'export', 'contact', 'link', f'{company.name} linked by hand to Xero contact '
                         f'{contact.get("Name")} by {request.user}', ref=company.name, pk=company.pk,
                         xero_id=contact['ContactID'])
                for model in _orders_for(company):
                    _clear_contact_holds(model)
            elif action == 'push':
                meta = xmeta(company)
                meta['contact_hash'] = ''  # force a difference so details are sent
                write_meta(company, meta)
                sync.contacts.push_update(company)
            elif action == 'balance':
                meta = xmeta(company)
                if meta.get('contact_id'):
                    sync._store_balance(company, sync.client.get_contact(meta['contact_id']).get('Balances') or {})
            else:
                sync.contacts.ensure(company, create=(action == 'create') or None)
            company.refresh_from_db()
        return Response(self._payload(company))


class PartXeroView(XeroAPIView):
    """GET → the part's Xero item link. POST {action: link|create|unlink}."""

    def _payload(self, part, sync=None):
        meta = xmeta(part)
        data = {
            'pk': part.pk,
            'name': part.full_name,
            'xero': {
                'linked': bool(meta.get('item_code')),
                'item_code': meta.get('item_code'),
                'item_id': meta.get('item_id'),
                'url': meta.get('url'),
                'synced': meta.get('synced'),
            },
            'connected': XeroConnection.get_solo().is_connected,
        }
        if sync is not None:
            data['code'] = sync.items.raw_code(part)
        return data

    def get(self, request, pk):
        from part.models import Part

        from .sync import XeroSync

        if not request.user.has_perm('part.view_part'):
            return Response({'error': 'Permission denied'}, status=403)
        return Response(self._payload(Part.objects.get(pk=pk), XeroSync(get_plugin())))

    def post(self, request, pk):
        from part.models import Part

        from .mapping import write_meta
        from .sync import XeroSync

        if not request.user.has_perm('part.change_part'):
            return Response({'error': 'Permission denied'}, status=403)
        part = Part.objects.get(pk=pk)
        sync = XeroSync(get_plugin())
        action = (request.data or {}).get('action', 'link')
        if action == 'unlink':
            meta = xmeta(part)
            for key in ('item_code', 'item_id', 'url', 'synced'):
                meta.pop(key, None)
            write_meta(part, meta)
        else:
            code = sync.items.raw_code(part)
            if not code:
                raise XeroError('This part has no usable item code (empty, or longer than 30 characters)')
            sync.client.ensure_token()
            item = sync.items.codes.get(code)
            if item:
                sync.items.link_part(part, item)
            elif action == 'create':
                sync.items.create_item(part, code)
            else:
                raise XeroError(f'No Xero item with code {code} — use "Create Xero item"')
            part.refresh_from_db()
        return Response(self._payload(part, sync))


class ShopQuotesView(XeroAPIView):
    """Web shop quote requests and their Xero quotes.

    GET ?q=&status=open|sent|closed|error  → list.  POST <quote request id>/<push|pull>/ → sync one now.
    """

    def get(self, request, pk=None, action=None):
        from .models import XeroQuoteLink
        from .shop_quotes import SHOP_CLOSED, SHOP_OPEN, shop_models

        if not request.user.has_perm(ORDER_PERMS['so'][0]):
            return Response({'error': 'Permission denied'}, status=403)
        models = shop_models()
        if models is None:
            return Response({'available': False, 'results': []})
        QuoteRequest, _ = models
        qs = QuoteRequest.objects.all().order_by('-created_at')
        q = (request.query_params.get('q') or '').strip()
        if q:
            ids = list(XeroQuoteLink.objects.filter(number__icontains=q).values_list('quote_request_id', flat=True))
            qs = qs.filter(Q(reference__icontains=q) | Q(name__icontains=q) | Q(company_name__icontains=q)
                           | Q(email__icontains=q) | Q(pk__in=ids))
        status = request.query_params.get('status')
        if status == 'open':
            qs = qs.filter(status__in=SHOP_OPEN)
        elif status == 'sent':
            qs = qs.filter(status='quoted')
        elif status == 'closed':
            qs = qs.filter(status__in=SHOP_CLOSED | {'accepted'})
        quotes = list(qs[:100])
        links = {link.quote_request_id: link for link in XeroQuoteLink.objects.filter(
            quote_request_id__in=[quote.pk for quote in quotes])}
        if status == 'error':
            quotes = [quote for quote in quotes if links.get(quote.pk) and links[quote.pk].error]
        results = []
        for quote in quotes:
            link = links.get(quote.pk)
            results.append({
                'id': quote.pk,
                'reference': quote.reference,
                'status': quote.get_status_display(),
                'shop_status': quote.status,
                'customer': quote.company_name or quote.name,
                'valid_until': quote.valid_until.isoformat() if quote.valid_until else None,
                'created': quote.created_at.isoformat(),
                'sales_order_id': quote.sales_order_id,
                'xero': link.to_dict() if link else None,
            })
        return Response({'available': True, 'results': results})

    def post(self, request, pk=None, action=None):
        from .shop_quotes import shop_models
        from .sync import XeroSync

        if not request.user.has_perm(ORDER_PERMS['so'][1]):
            return Response({'error': 'Permission denied'}, status=403)
        models = shop_models()
        if models is None:
            raise XeroError('The web shop plugin is not installed or not active')
        quote = models[0].objects.get(pk=pk)
        sync = XeroSync(get_plugin())
        sync.client.ensure_token()
        if action == 'push':
            ok, message = sync.shop_quotes.safe(sync.shop_quotes.export_quote, quote, force=True)
        elif action == 'pull':
            ok, message = sync.shop_quotes.safe(sync.shop_quotes.pull, quote)
        else:
            return Response({'error': 'Unknown action'}, status=400)
        return Response({'ok': ok, 'message': message}, status=200 if ok else 400)


def _orders_for(company):
    from order.models import PurchaseOrder, SalesOrder

    return list(PurchaseOrder.objects.filter(supplier=company)) + list(SalesOrder.objects.filter(customer=company))


def _clear_contact_holds(order):
    """After a contact is linked by hand, orders held for that reason are retried on the next sync."""
    from .mapping import write_meta

    meta = xmeta(order)
    changed = False
    for target in (meta, meta.get('bill') or {}):
        reason = (target.get('held') or {}).get('reason', '')
        if 'contact' in reason.lower():
            target.pop('held', None)
            changed = True
    if changed:
        write_meta(order, meta)


class CompanyCandidatesView(XeroAPIView):
    """Xero contacts that might be this company, for linking by hand (?q= to search another name)."""

    def get(self, request, pk):
        from company.models import Company

        from .mapping import contact_conflicts
        from .sync import XeroSync

        if not request.user.has_perm('company.view_company'):
            return Response({'error': 'Permission denied'}, status=403)
        company = Company.objects.get(pk=pk)
        sync = XeroSync(get_plugin())
        sync.client.ensure_token()
        term = (request.query_params.get('q') or company.name).strip()
        rows = []
        for contact in sync.client.search_contacts(term)[:25]:
            if contact.get('ContactStatus', 'ACTIVE') == 'ARCHIVED':
                continue
            rows.append({
                'contact_id': contact.get('ContactID'),
                'name': contact.get('Name'),
                'email': contact.get('EmailAddress'),
                'tax_number': contact.get('TaxNumber'),
                'conflicts': contact_conflicts(company, contact),
            })
        return Response({'query': term, 'results': rows})


class WriteModeView(XeroAPIView):
    """POST {"mode": "dry_run" | "live" | "paused", "confirm": true} - the kill switch and go-live (staff)."""

    def post(self, request):
        mode = (request.data or {}).get('mode')
        if mode not in ('dry_run', 'live', 'paused'):
            return Response({'error': 'mode must be dry_run, live or paused'}, status=400)
        if mode == 'live' and not (request.data or {}).get('confirm'):
            return Response({'error': 'Going live writes to Xero - send confirm: true'}, status=400)
        plugin = get_plugin()
        plugin.set_setting('WRITE_MODE', mode, user=request.user)
        XeroSyncLog.objects.create(level='warning' if mode == 'live' else 'info', direction='system',
                                   doc_type='system', action='mode',
                                   message=f'Write mode set to {mode.replace("_", " ").upper()} by {request.user}')
        return Response(connection_info(plugin, request))


class AuditView(XeroAPIView):
    """Every write to Xero (and every dry-run write). ?id= for one record with its request and response."""

    def get(self, request):
        from .models import XeroWriteAudit

        if not request.user.is_staff:
            return Response({'error': 'Permission denied'}, status=403)
        if request.query_params.get('id'):
            record = XeroWriteAudit.objects.get(pk=request.query_params['id'])
            return Response({**record.to_dict(), 'request_body': record.request_body,
                             'response_body': record.response_body, 'idempotency_key': record.idempotency_key})
        qs = XeroWriteAudit.objects.all()
        if request.query_params.get('mode'):
            qs = qs.filter(mode=request.query_params['mode'])
        if request.query_params.get('ref'):
            qs = qs.filter(inventree_ref__icontains=request.query_params['ref'])
        try:
            limit = min(int(request.query_params.get('limit', 50)), 500)
        except ValueError:
            limit = 50
        return Response([record.to_dict() for record in qs[:limit]])


class CheckView(XeroAPIView):
    """GET → last configuration check. POST → run the read-only configuration check now (staff)."""

    def get(self, request):
        last = XeroSyncLog.objects.filter(action='check').first()
        return Response({'last': last.to_dict() if last else None})

    def post(self, request):
        from .selftest import run_check

        return Response(run_check(get_plugin()).as_dict())


class SelfTestView(XeroAPIView):
    """GET → last self-test report. POST {"confirm": "DEMO"} → queue the live self-test (Demo Company only, staff)."""

    def get(self, request):
        last = XeroSyncLog.objects.filter(action='selftest').first()
        return Response({'last': last.to_dict() if last else None})

    def post(self, request):
        from InvenTree.tasks import offload_task

        if (request.data or {}).get('confirm') != 'DEMO':
            return Response({'error': 'Type DEMO to confirm test documents may be written to the Demo Company'},
                            status=400)
        queued = offload_task('plugin.registry.call_plugin_function', 'xero', 'run_selftest_task', force_async=True)
        if not queued:
            return Response({'error': 'Could not queue the self-test - is the background worker running?'}, status=503)
        return Response({'queued': True})
