"""Tests for lb.config (no database needed)."""

from psycopg.conninfo import conninfo_to_dict

from lb.config import database_url


def test_password_with_special_characters_is_not_misparsed(monkeypatch):
    # Regression: a URL-style string split 'p@ss:w/rd#1' into host 'ss', port 'w'.
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1 x'y")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setenv("POSTGRES_PORT", "6543")

    parsed = conninfo_to_dict(database_url())

    assert parsed["password"] == "p@ss:w/rd#1 x'y"
    assert parsed["host"] == "db.example"
    assert parsed["port"] == "6543"
