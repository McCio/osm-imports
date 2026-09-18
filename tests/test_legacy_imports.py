"""Tests for legacy-import (changeset-source) detection."""

from __future__ import annotations

from dbsn.legacy_imports import compile_patterns, is_legacy_import, layered_patterns


_PATTERNS = compile_patterns([
    {"changeset_source": r"Regione Veneto, License IODL-2\.0"},
    {"changeset_source": r"Regione Veneto", "user": r"^DarkSwan_Import$"},
])


def test_match_source_only():
    assert is_legacy_import({"source": "Regione Veneto, License IODL-2.0 (http://…)",
                             "user": "anyone"}, _PATTERNS)


def test_user_gate_rejects_wrong_user():
    # second pattern needs source AND user
    assert not is_legacy_import({"source": "Ortofoto Regione Veneto",
                                 "user": "phaiderikos"}, _PATTERNS)


def test_user_gate_accepts_right_user():
    assert is_legacy_import({"source": "Ortofoto Regione Veneto",
                             "user": "DarkSwan_Import"}, _PATTERNS)


def test_no_match_other_source():
    assert not is_legacy_import({"source": "Bing", "user": "x"}, _PATTERNS)


def test_none_or_empty_patterns():
    assert not is_legacy_import(None, _PATTERNS)
    assert not is_legacy_import({"source": "Regione Veneto"}, [])


def test_regex_partial_match():
    # re.search semantics: substring match, not fullmatch
    assert is_legacy_import({"source": "source=Regione Veneto CTR"}, _PATTERNS) is False


def test_layered_patterns_append_across_layers():
    config = {
        "legacy_imports": {
            "patterns": [{"changeset_source": "global"}],
            "regions": {"Veneto": {"patterns": [{"changeset_source": "rv"}]}},
            "provinces": {"VE": {"patterns": [{"changeset_source": "ve"}]}},
            "areas": {"marghera": {"patterns": [{"changeset_source": "mgh"}]}},
        }
    }
    got = layered_patterns(config, "VE", "Veneto", "_marghera")
    assert [p["changeset_source"] for p in got] == ["global", "rv", "ve", "mgh"]


def test_layered_patterns_empty_config():
    assert layered_patterns({}, "VE", "Veneto", "") == []
