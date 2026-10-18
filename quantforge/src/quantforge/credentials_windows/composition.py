"""The single approved production composition site for CREDENTIAL-002.

The build function lazily assembles the production native seam, the concrete
backend and the CredentialBoundary on every call and returns a fresh
independent boundary.  It installs nothing globally, replaces nothing and
grants no consumer authority.  See ``tasks/CREDENTIAL-002.md``.
"""

from __future__ import annotations

from quantforge.credentials import CredentialBoundary

from .backend import WindowsCredentialManagerBackend
from .native import Advapi32CredentialReadSeam


def build_credential_boundary():
    """Return a fresh boundary over the production Windows vault seam."""
    seam = Advapi32CredentialReadSeam()
    backend = WindowsCredentialManagerBackend(seam=seam)
    return CredentialBoundary(backend=backend)
