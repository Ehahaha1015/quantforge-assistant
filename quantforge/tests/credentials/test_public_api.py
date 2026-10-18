"""Oracle AE - the frozen 18-name public surface and error taxonomy."""

from __future__ import annotations

import importlib
import inspect

from quantforge import credentials

FROZEN_SURFACE = [
    "CredentialScope",
    "CredentialReference",
    "CredentialFacts",
    "CredentialPurpose",
    "CredentialBackend",
    "RejectingCredentialBackend",
    "CredentialBoundary",
    "CredentialLease",
    "SecretMaterial",
    "CredentialError",
    "CredentialArgumentError",
    "CredentialProviderError",
    "CredentialProviderIdentityError",
    "CredentialUnavailableError",
    "CredentialRejectedError",
    "CredentialScopeMismatchError",
    "CredentialLeaseError",
    "CredentialLeaseExpiredError",
]

PACKAGE_MODULES = (
    "__init__",
    "boundary",
    "errors",
    "models",
    "provider",
    "secrets",
)


def test_public_surface_is_the_exact_frozen_order() -> None:
    assert credentials.__all__ == FROZEN_SURFACE


def test_every_exported_name_is_a_class_with_a_docstring() -> None:
    for name in FROZEN_SURFACE:
        exported = getattr(credentials, name)
        assert inspect.isclass(exported), name
        assert exported.__doc__, name


def test_no_package_module_defines_an_extra_public_name() -> None:
    for module_name in PACKAGE_MODULES:
        module = importlib.import_module(
            f"quantforge.credentials.{module_name}"
        )
        defined = [
            name
            for name, value in vars(module).items()
            if not name.startswith("_")
            and getattr(value, "__module__", None) == module.__name__
        ]
        assert set(defined) <= set(FROZEN_SURFACE), module_name


def test_error_taxonomy_is_frozen() -> None:
    assert issubclass(credentials.CredentialError, ValueError)
    assert issubclass(
        credentials.CredentialArgumentError, credentials.CredentialError
    )
    assert issubclass(
        credentials.CredentialProviderError, credentials.CredentialError
    )
    assert issubclass(
        credentials.CredentialProviderIdentityError,
        credentials.CredentialProviderError,
    )
    assert issubclass(
        credentials.CredentialUnavailableError,
        credentials.CredentialProviderError,
    )
    assert issubclass(
        credentials.CredentialRejectedError,
        credentials.CredentialProviderError,
    )
    assert issubclass(
        credentials.CredentialScopeMismatchError,
        credentials.CredentialProviderError,
    )
    assert issubclass(
        credentials.CredentialLeaseError, credentials.CredentialError
    )
    assert issubclass(
        credentials.CredentialLeaseExpiredError,
        credentials.CredentialLeaseError,
    )
