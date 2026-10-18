"""Structured source-identity parsing without network or transport imports."""

from pydantic import AnyHttpUrl


def bounded_https_source(value: str) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(character.isspace() for character in value)
    ):
        return False
    try:
        address = AnyHttpUrl(value)
    except ValueError:
        return False
    return (
        address.scheme == "https"
        and bool(address.host)
        and address.username is None
        and address.password is None
        and address.fragment is None
    )
