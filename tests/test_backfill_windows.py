"""A backfill window is recorded only when its searches mostly worked.

Recording a window whatever happened let a dead browser (2026-10-23, after a
reboot) and then a dropped network mark 155 weeks swept with nothing in them,
and the run exited 0 as if the backfill were complete.
"""
import importlib.util
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
_spec = importlib.util.spec_from_file_location("scrape", _root / "scrape.py")
scrape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrape)


def test_every_search_failing_leaves_the_window_unrecorded():
    assert scrape.mostly_failed(28, 28)


def test_a_majority_failing_leaves_it_unrecorded():
    assert scrape.mostly_failed(15, 28)


def test_one_dropped_search_still_records_it():
    # The 2023-02-20 week lost one search to a brief disconnect and still
    # gathered a normal week's posts.
    assert not scrape.mostly_failed(1, 28)


def test_half_failing_still_records_it():
    assert not scrape.mostly_failed(14, 28)


def test_no_searches_attempted_is_not_a_failure():
    # A time budget can end the window before any search; the existing
    # behaviour (record it) is left alone.
    assert not scrape.mostly_failed(0, 0)


def _refused(code=1675004):
    return {"errors": [{"code": code, "message": "Rate limit exceeded", "severity": "CRITICAL"}]}


def test_a_refused_side_query_is_passed_over_when_ignored():
    errs = scrape.check_for_refusal(
        _refused(), "FBYRPTimeLimitsEnforcementQuery", scrape.BACKFILL_IGNORED_LIMITS)
    assert errs and errs[0][0] == 1675004


def test_a_refused_search_still_stops_the_backfill():
    try:
        scrape.check_for_refusal(
            _refused(), "SearchCometResultsPaginatedResultsQuery",
            scrape.BACKFILL_IGNORED_LIMITS)
    except scrape.RateLimited:
        return
    raise AssertionError("a refused search must raise RateLimited")


def test_the_side_query_still_stops_other_passes():
    # The comments pass and the feed crawl pass no ignore set.
    try:
        scrape.check_for_refusal(_refused(), "FBYRPTimeLimitsEnforcementQuery")
    except scrape.RateLimited:
        return
    raise AssertionError("without an ignore set every refusal must raise")
