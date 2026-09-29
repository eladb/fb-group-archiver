"""Place redaction: the cases that actually escaped, and the ones that must not go.

The failure classes come from a real measurement -- `accuracy.py freecheck`
flagged 36 of 300 pilot items as still carrying an identifier -- but every string
here is constructed to reproduce a class. None is taken from the corpus: this
repository is public, and a real fragment plus "PANDAS parent group" points at a
family.
"""
import importlib.util
import pathlib
import sys

_spec = importlib.util.spec_from_file_location(
    "redact_sample", pathlib.Path(__file__).resolve().parent.parent / "redact_sample.py")
rs = importlib.util.module_from_spec(_spec)
sys.modules["redact_sample"] = rs
_spec.loader.exec_module(rs)


def red(text):
    return rs.redact(text, [])[0]


class TestPlacesThatEscaped:
    """Each of these survived the committed redactor and reached a third party."""

    def test_shouted_state_name(self):
        # The state was in REGIONS all along; the pattern was case-sensitive.
        assert "OREGON" not in red("OREGON PARENTS!! anyone know a specialist?")

    def test_misspelled_city_after_preposition(self):
        # Lowercase defeated the capital-letter requirement, and the misspelling
        # defeated the region list. Both, in one four-word phrase.
        out = red("any experience with a clinic in sacramneto califrnia?")
        assert "sacramneto" not in out and "califrnia" not in out

    def test_bare_city_without_preposition(self):
        # "the <City> area" carries no locative preposition.
        assert "Tulsa" not in red("is there a specialist in the Tulsa area?")

    def test_city_mid_sentence(self):
        assert "Seattle" not in red("Looking at Seattle Children's for this.")


class TestOrdinaryProseIsLeftAlone:
    """Pre-existing over-redaction: the rule ate any capitalised word after a
    locative preposition, so months, weekdays and Facebook all became [PLACE].
    That protected nobody and removed the timing the annotation layer asks for."""

    CASES = [
        "Swim lessons start Monday and run through June.",
        "It got worse in February and settled in April.",
        "The tics began in Kindergarten.",
        "I first read about it from Facebook.",
        "We take strength from God most days.",
        "He answers in English and Spanish.",
        "She came back from Grandma on Sunday.",
        "He has been in bed since lunch.",
        "We have been in remission since autumn.",
        "He came home from school exhausted.",
        "We are still full of hope.",
        "She wore her jersey to the game.",
        "He is doing well in reading this year.",
    ]

    def test_unchanged(self):
        for text in self.CASES:
            assert red(text) == text, text


class TestPlacesStillGo:
    CASES = [
        ("We live in Texas.", "Texas"),
        ("We are near Tulsa.", "Tulsa"),
        ("Anyone in New Jersey?", "Jersey"),
        ("Looking for someone in Cincinnati.", "Cincinnati"),
        ("We are in Florida now.", "Florida"),
    ]

    def test_redacted(self):
        for text, gone in self.CASES:
            out = red(text)
            assert gone not in out, text
            assert "[PLACE]" in out, text


def test_place_runs_collapse_without_eating_punctuation():
    # "New Jersey" is hit by the region rule and the locative rule in turn; the
    # greedy collapse used to leave "in [PLACE] ?".
    assert red("Anyone seen someone in New Jersey?") == "Anyone seen someone in [PLACE]?"
