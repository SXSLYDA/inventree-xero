"""Django app configuration for the Xero plugin (loaded through InvenTree's AppMixin)."""

from django.apps import AppConfig


class InvenTreeXeroConfig(AppConfig):
    name = 'inventree_xero'
    label = 'inventree_xero'
    verbose_name = 'Xero Integration'
    default_auto_field = 'django.db.models.AutoField'
