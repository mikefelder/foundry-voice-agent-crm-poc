import pytest

from crm_companion.crm.soql import (
    MAX_LITERAL_LENGTH,
    MAX_SEARCH_TERM_LENGTH,
    UnsafeQueryInput,
    record_id,
    soql_literal,
    sosl_term,
)

VALID_15 = "001A000001abcDE"
VALID_18 = "001A000001abcDEFGH"


class TestRecordId:
    @pytest.mark.parametrize("value", [VALID_15, VALID_18, "0" * 15, "aA9" * 6])
    def test_accepts_valid_ids(self, value):
        assert record_id(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "001A00000",  # too short
            "001A000001abcDEFGHI",  # 19 chars
            "001A000001abcD",  # 14 chars
            "001A000001abcDEFG",  # 17 chars
            "001A000001abc-E",  # hyphen
            "001A000001abc E",  # space
            "001A000001abc'E",  # quote
            "001A000001abcDE' OR Id != '",
        ],
    )
    def test_rejects_malformed_ids(self, value):
        with pytest.raises(UnsafeQueryInput):
            record_id(value)

    @pytest.mark.parametrize("value", [None, 1, b"001A000001abcDE", ["x"]])
    def test_rejects_non_strings(self, value):
        with pytest.raises(UnsafeQueryInput):
            record_id(value)

    def test_error_names_the_field(self):
        with pytest.raises(UnsafeQueryInput, match="account_id"):
            record_id("nope", field="account_id")


class TestSoqlLiteral:
    def test_wraps_in_quotes(self):
        assert soql_literal("Acme") == "'Acme'"

    def test_escapes_single_quote(self):
        assert soql_literal("O'Brien") == r"'O\'Brien'"

    def test_escapes_backslash_before_quote(self):
        # A trailing backslash must not escape the closing quote.
        assert soql_literal("path\\") == r"'path\\'"

    def test_escapes_double_quote(self):
        assert soql_literal('say "hi"') == r"'say \"hi\"'"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a\nb", r"'a\nb'"),
            ("a\rb", r"'a\rb'"),
            ("a\tb", r"'a\tb'"),
            ("a\bb", r"'a\bb'"),
            ("a\fb", r"'a\fb'"),
        ],
    )
    def test_escapes_whitespace_controls(self, raw, expected):
        assert soql_literal(raw) == expected

    def test_neutralises_classic_injection(self):
        escaped = soql_literal("x' OR Name != '")
        assert escaped == r"'x\' OR Name != \''"
        # No bare quote remains that could terminate the literal early.
        assert "\\'" in escaped
        assert escaped.count("'") - escaped.count("\\'") == 2  # only the wrapping pair

    def test_rejects_control_characters(self):
        with pytest.raises(UnsafeQueryInput, match="control characters"):
            soql_literal("bad\x00value")

    def test_enforces_length_cap(self):
        soql_literal("a" * MAX_LITERAL_LENGTH)
        with pytest.raises(UnsafeQueryInput, match="exceeds"):
            soql_literal("a" * (MAX_LITERAL_LENGTH + 1))

    def test_rejects_non_strings(self):
        with pytest.raises(UnsafeQueryInput):
            soql_literal(42)


class TestSoslTerm:
    def test_passes_through_plain_text(self):
        assert sosl_term("Contoso Building") == "Contoso Building"

    @pytest.mark.parametrize("char", list("?&|!{}[]()^~*:\\\"'+-"))
    def test_escapes_every_reserved_character(self, char):
        assert sosl_term(f"ab{char}") == f"ab\\{char}"

    def test_collapses_whitespace(self):
        assert sosl_term("  Contoso   Building  ") == "Contoso Building"

    def test_rejects_terms_below_minimum_length(self):
        with pytest.raises(UnsafeQueryInput, match="at least"):
            sosl_term("a")

    def test_rejects_whitespace_only(self):
        with pytest.raises(UnsafeQueryInput, match="at least"):
            sosl_term("    ")

    def test_escaped_length_may_exceed_input_length(self):
        # Escaping expands; the cap applies to the raw input.
        assert sosl_term("a" * MAX_SEARCH_TERM_LENGTH)
        with pytest.raises(UnsafeQueryInput, match="exceeds"):
            sosl_term("a" * (MAX_SEARCH_TERM_LENGTH + 1))

    def test_neutralises_sosl_breakout(self):
        assert sosl_term("Acme} RETURNING User(Id)") == "Acme\\} RETURNING User\\(Id\\)"
