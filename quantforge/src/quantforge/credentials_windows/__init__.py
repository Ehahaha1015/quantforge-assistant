"""Supported package surface for the CREDENTIAL-002 credential backend.

The package-supported public API is exactly ``build_credential_boundary``.
That export is not an authority boundary: the repository-wide static rules
keep every production caller and importer empty until a reviewed consumer
amendment.  See ``tasks/CREDENTIAL-002.md``.
"""

from __future__ import annotations

from .composition import build_credential_boundary

__all__ = ("build_credential_boundary",)
