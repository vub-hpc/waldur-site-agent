"""Shared setup for the sofia-storage plugin tests."""
import optparse

# vsc.utils.generaloption does `from optparse import gettext`, which fails on
# interpreters where optparse.gettext has been removed. Restore it before any
# plugin module (which imports vsc) is collected.
if not hasattr(optparse, "gettext"):
    import gettext

    optparse.gettext = gettext.gettext
