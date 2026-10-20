"""Django admin registration — sync log browsing and connection overview."""

from django.contrib import admin

from .models import XeroConnection, XeroQuoteLink, XeroSyncLog, XeroWriteAudit


@admin.register(XeroSyncLog)
class XeroSyncLogAdmin(admin.ModelAdmin):
    list_display = ('created', 'level', 'direction', 'doc_type', 'action', 'inventree_ref', 'xero_number', 'short_message')
    list_filter = ('level', 'direction', 'doc_type', 'action')
    search_fields = ('inventree_ref', 'xero_number', 'xero_id', 'message')
    date_hierarchy = 'created'
    readonly_fields = [f.name for f in XeroSyncLog._meta.fields]

    @admin.display(description='Message')
    def short_message(self, obj):
        return (obj.message or '')[:120]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(XeroConnection)
class XeroConnectionAdmin(admin.ModelAdmin):
    list_display = ('tenant_name', 'tenant_id', 'connected_at', 'connected_by', 'last_sync_finished', 'last_sync_ok')
    exclude = ('access_token', 'refresh_token')
    readonly_fields = (
        'tenant_id', 'tenant_name', 'short_code', 'tenants', 'expires_at', 'scopes',
        'connected_at', 'connected_by', 'last_sync_started', 'last_sync_finished',
        'last_sync_ok', 'last_sync_message',
    )

    def has_add_permission(self, request):
        return False


@admin.register(XeroQuoteLink)
class XeroQuoteLinkAdmin(admin.ModelAdmin):
    list_display = ('reference', 'shop_status', 'number', 'status', 'total', 'invoiced', 'final', 'synced')
    list_filter = ('status', 'shop_status', 'final')
    search_fields = ('reference', 'number', 'xero_id')
    readonly_fields = [f.name for f in XeroQuoteLink._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(XeroWriteAudit)
class XeroWriteAuditAdmin(admin.ModelAdmin):
    list_display = ('created', 'mode', 'method', 'path', 'doc_type', 'inventree_ref', 'response_status', 'ok')
    list_filter = ('mode', 'ok', 'method', 'doc_type')
    search_fields = ('inventree_ref', 'path', 'idempotency_key', 'error')
    date_hierarchy = 'created'
    readonly_fields = [f.name for f in XeroWriteAudit._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
