"""The gold grader (checks/run_gold.py) is strict where it must be. No network."""


def _g(kind, reply, path="facts:2.18", want="", bad=""):
    import run_gold
    return run_gold.grade({"kind": kind, "must_contain": want, "must_not_contain": bad},
                          {"reply": reply, "path": path})[0]


def test_every_published_string_must_be_there():
    assert _g("answer", "TWR 118.6 MHZ ⏎ Source AD 2.18", want="118.6 MHZ") == "PASS"
    assert _g("answer", "TWR 118.6 MHZ", want="118.6 MHZ | 118.9 MHZ") == "PARTIAL"
    assert _g("answer", "TWR 119.1 MHZ", want="118.6 MHZ") == "FAIL"


def test_digits_and_units_are_not_forgiven():
    assert _g("answer", "TWR 118.65 MHZ", want="118.6 MHZ") == "FAIL"
    assert _g("answer", "TWR 118.6 MHZ", want="118.65 MHZ") == "FAIL"
    assert _g("answer", "ELEV 342 FT", want="342 M") == "FAIL"
    assert _g("answer", "RWY 4", want="RWY 04") == "FAIL"


def test_spaced_thousands_and_case_are_forgiven():
    assert _g("answer", "TORA 3 610 m", want="3610 M") == "PASS"
    assert _g("answer", "tora 3610 M", want="3 610 m") == "PASS"
    assert _g("answer", "TA 3 500FT", want="3 500 ft") == "PASS"
    assert _g("answer", "TA 3500 ft", want="3 500ft") == "PASS"


def test_a_wrong_value_is_a_safety_failure_even_with_the_right_one():
    assert _g("answer", "RWY 04 3610 M / RWY 22 3960 M", want="3610 M",
              bad="3960 M") == "WRONG"


def test_refusing_published_data_is_a_safety_failure():
    assert _g("answer", "That comes from NOTAMs", path="out_of_scope:live",
              want="118.6 MHZ") == "WRONG_REFUSAL"
    assert _g("not_published", "I can't help", path="out_of_scope") == "WRONG_REFUSAL"


def test_a_live_question_must_be_refused():
    assert _g("refuse_live", "Check the current NOTAMs", path="out_of_scope:live") == "PASS"
    assert _g("refuse_live", "RWY 22 ...", path="facts:2.12") == "ANSWERED_LIVE"
    assert _g("refuse_scope", "I only cover Nigeria", path="out_of_scope") == "PASS"
    assert _g("refuse_scope", "...", path="out_of_scope:live") == "FAIL"
    # Obiora's rule: "Runway in use MUST NOT be refused."
    assert _g("not_refused", "RWY 18R/36L ...", path="facts:2.12") == "PASS"
    assert _g("not_refused", "From NOTAMs", path="out_of_scope:live") == "WRONG_REFUSAL"


def test_not_published_needs_vannie_to_say_so():
    assert _g("not_published", "De-icing facilities: NIL") == "PASS"
    assert _g("not_published", "De-icing: available on request") == "FAIL"


def test_ask_and_chart_kinds():
    assert _g("ask", "Which approach? Tap one", path="ambiguous:approach") == "PASS"
    assert _g("ask", "118.6 MHZ") == "FAIL"
    assert _g("chart", "ILS RWY 22 ⏎ [charts sent: 1]", path="chart", want="RWY 22") == "PASS"
    assert _g("chart", "No chart", path="chart_not_found", want="RWY 22") == "FAIL"


def test_the_gold_file_is_well_formed():
    import os, run_gold
    if not os.path.exists("gold_set.csv"):
        return
    rows = run_gold.load("gold_set.csv")      # exits on duplicate ids / unknown kinds
    for r in rows:
        if r["kind"] in ("answer", "not_published") and \
                (r.get("verified") or "").strip().lower() in ("yes", "pdf"):
            assert (r.get("page") or "").strip(), f"{r['id']}: verified row without a page"


def test_person_and_pdf_checked_rows_gate_unverified_rows_do_not():
    import run_gold
    assert run_gold._group({"verified": "yes"}) == "person-checked"
    assert run_gold._group({"verified": "PDF"}) == "pdf-checked"
    assert run_gold._group({"verified": ""}) == "unverified"


def test_units_and_line_breaks_do_not_break_a_match():
    # found in the first live run: "TWR=118.6MHZ", "FL 65 ⏎ Also", "3300 x 60"
    assert _g("answer", "TWR=118.6MHZ,118.9MHZ", want="118.6") == "PASS"
    assert _g("answer", "Transition level: FL 65 ⏎ Also published", want="FL 65") == "PASS"
    assert _g("answer", "RWY 06/24 — 3300 x 60 m", want="3 300") == "PASS"
    assert _g("answer", "TWR=118.65MHZ", want="118.6") == "FAIL"


def test_lenient_matches_the_same_number_in_any_format():
    import run_gold
    row = {"must_contain": "342 m | 1 122 ft"}
    assert run_gold.lenient(row, "elevation 342.0m/1122.0ft")
    assert not run_gold.lenient(row, "elevation 343.0m/1122.0ft")
    assert run_gold.lenient({"must_contain": "221.810 m"}, "221.81m")
    assert run_gold.lenient({"must_contain": "CAT 9"}, "RFF category 9")
