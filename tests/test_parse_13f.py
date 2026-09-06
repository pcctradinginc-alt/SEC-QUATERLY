"""Prior-quarter matching must survive our own renames and CIK corrections."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import parse_13f as p13  # noqa: E402

PRIOR = {"filers": {
    "TCI Fund (Chris Hohn)": {"cik": "0001649339", "reported_aum_k": 100, "positions": [{"cusip": "A"}]},
    "Coatue (Laffont)":      {"cik": "0001766502", "reported_aum_k": 200, "positions": [{"cusip": "B"}]},
    "Yale University":       {"cik": "0000938582", "reported_aum_k": 300, "positions": [{"cusip": "C"}]},
}}


def test_rename_keeps_the_prior_quarter():
    """Relabelling a filer must not turn every carried-over holding into NEW."""
    m = p13._match_prior_filer(PRIOR, "Scion Asset Management (Burry)", "0001649339")
    assert m is not None and m["positions"] == [{"cusip": "A"}]


def test_corrected_cik_drops_the_prior_quarter():
    """Coatue kept its label while its CIK was corrected away from Chewy's; a
    name-keyed join would diff Chewy's book against Coatue's."""
    assert p13._match_prior_filer(PRIOR, "Coatue (Laffont)", "0001135730") is None


def test_unchanged_filer_and_unknown_filer():
    assert p13._match_prior_filer(PRIOR, "Yale University", "0000938582") is not None
    assert p13._match_prior_filer(PRIOR, "DUMAC, Inc.", "0001584258") is None
    assert p13._match_prior_filer(None, "Yale University", "0000938582") is None


def test_falls_back_to_name_when_prior_has_no_cik():
    prior = {"filers": {"Old Fund": {"reported_aum_k": 1, "positions": []}}}
    assert p13._match_prior_filer(prior, "Old Fund", "0000000001") is not None
