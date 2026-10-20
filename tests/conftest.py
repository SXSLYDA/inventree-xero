"""Unit tests run without InvenTree: configure a bare Django so the helper modules import."""

import os
import sys

import django
from django.conf import settings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not settings.configured:
    settings.configure(USE_TZ=True, INSTALLED_APPS=[], SECRET_KEY='unit-test-secret')
    django.setup()

collect_ignore = ['e2e_inventree.py', 'fake_xero.py']
