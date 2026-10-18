"""Frozen canonicalization rule tests."""

from __future__ import annotations

from quantforge.operator.canonical import normalize_sql


def test_normalize_sql_removes_comments_and_normalizes_outside_literals() -> None:
    source = "CREATE TABLE x (a TEXT); -- comment\n"
    assert normalize_sql(source) == "create table x(a text)"


def test_normalize_sql_preserves_single_quoted_literals() -> None:
    source = "CREATE TABLE x (a TEXT CHECK (a IN ('A B','C')));"
    assert normalize_sql(source) == (
        "create table x(a text check(a in('A B','C')))"
    )
