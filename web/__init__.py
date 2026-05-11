"""Vitriol web application package.

Single source of truth for the version string lives in
``app/__version__.py`` — re-export it here so templates that import
``web.__version__`` always agree with the installer + the desktop
``About`` dialog. Bumping the canonical file is the only place a
release version change needs to happen.
"""
from app.__version__ import __version__  # noqa: F401 — re-export
