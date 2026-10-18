"""Deterministic market validation, strategies and replay engines.

The evidence artifacts in this package carry a payload field literally named
``schema`` (for example ``frontier-durable-record-v1``). Its canonical JSON is
hash-verified and already persisted, so the name cannot change without a
migration. Pydantic v2 warns at class creation because that field name shadows
the deprecated ``BaseModel.schema`` method; nothing here calls that method, so
the warning is noise on every container start and on every test run.

The filter below matches the exact warning text, so unrelated field-shadowing
warnings still surface. It is installed here because importing any submodule of
this package executes this initialiser first.
"""

from __future__ import annotations

import warnings

warnings.filterwarnings(
    "ignore",
    message=r'Field name "schema" in ".*" shadows an attribute in parent ".*"',
    category=UserWarning,
)


def main() -> None:
    from .product.cli import main as product_main

    product_main()
