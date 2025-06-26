from django.utils.translation import activate, trans_real

import pytest


pytestmark = pytest.mark.django_db


def test_amo_locale_not_in_django():
    """
    We load gettext catalogs in this order:
        django/locale/django.po
        amo/locale/django.po

    If Django doesn't have a locale, it returns the en-us catalog as a
    fallback.  But then we take that catalog and merge in our z-messages.po.
    That's no good because we just mixed some other locale into en-us.

    This test will be invalid once Django gets an mn locale.
    """
    activate('mn')
    en = trans_real._translations['en-US']
    mn = trans_real._translations['mn']
    assert en != mn
    assert en._catalog != mn._catalog
