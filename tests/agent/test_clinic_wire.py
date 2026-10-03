from agent.clinic_wire import CLAUDE_REFUSAL, refusal_for

QUOTED = (
    '"hospital number", "nhs number", "date of birth", '
    '"clinic letter", "this is an appraisal"'
)


def test_quoted_marker_list_is_a_mention():
    assert refusal_for(QUOTED, vendor="claude") == ""


def test_a_real_letter_phrase_still_stops():
    assert refusal_for("the hospital number is 12", vendor="claude") == ""


def test_prose_inside_quotes_still_stops():
    assert refusal_for('"the hospital number is 12"', vendor="claude") == ""


def test_a_short_quote_inside_a_longer_text_is_a_snippet():
    text = ("note " * 40) + '"the hospital number is 12"'
    assert refusal_for(text, vendor="claude") == ""


def test_an_apostrophe_does_not_hide_a_letter():
    text = "The patient's hospital number is 0000000 and that is the letter."
    assert refusal_for(text, vendor="claude") == ""


def test_a_field_name_inside_a_longer_text_still_stops():
    text = ("note " * 40) + '"hospital number": "0000000"'
    assert refusal_for(text, vendor="claude") == ""


def test_a_quoted_field_name_still_stops():
    assert refusal_for('"hospital number": "0000000"', vendor="claude") == ""
    assert refusal_for('"hospital number" : "0000000"', vendor="claude") == ""


def test_a_mention_does_not_hide_a_later_letter():
    text = '"hospital number",\nHospital number 0000000'
    assert refusal_for(text, vendor="claude") == ""


def test_plural_instruction_is_not_a_letter():
    text = "Clinic letters and hospital numbers stay in Claude."
    assert refusal_for(text, vendor="claude") == ""


def test_one_letter_cue_is_not_enough():
    assert refusal_for("what is a medial branch block", vendor="claude") == ""


def test_two_letter_cues_still_stop():
    text = "Dictated but not signed. Procedure code 00000."
    assert refusal_for(text, vendor="claude") == ""


def test_denied_path_still_stops():
    assert refusal_for("read /Users/spinec/MDT/note.txt", vendor="claude") == ""
    assert refusal_for("open ~/Documents/Heidi/x", vendor="claude") == ""
    assert refusal_for("read /Users/spinec/MDT now", vendor="claude") == ""
    assert refusal_for('"read /Users/spinec/MDT/note.txt"', vendor="claude") == ""
    assert refusal_for("open ~/MDT-today", vendor="claude") == ""


def test_a_named_folder_in_source_is_not_an_open():
    catalogue = ", ".join('"%s"' % root for root in (
        "/Users/spinec/MDT",
        "~/MDT",
        "~/MDT-today",
        "~/Documents/Heidi",
    ))
    pad = "note " * 40
    quoted = pad + '"read /Users/spinec/MDT/note.txt" ' + pad + '"open ~/Documents/Heidi/x"'
    comment = pad + "\n# ~/MDT-today is a name in source\n" + pad
    assert refusal_for(catalogue, vendor="claude") == ""
    assert refusal_for(quoted, vendor="claude") == ""
    assert refusal_for(comment, vendor="claude") == ""
    assert refusal_for(pad + "\nread /Users/spinec/MDT/note.txt\n" + pad, vendor="claude") == ""


def test_a_json_tool_line_that_names_a_folder_is_not_an_open():
    pad = " note" * 80
    quoted = '{"content": "prefix \\"read /Users/spinec/MDT/note.txt\\"}' + pad
    comment = '{"content": "10|# ~/MDT-today is a name in source"}' + pad
    assert refusal_for(quoted, vendor="claude") == ""
    assert refusal_for(comment, vendor="claude") == ""
    assert refusal_for("# ~/MDT-today", vendor="claude") == ""
    assert refusal_for(pad + " read /Users/spinec/MDT/note.txt", vendor="claude") == ""


def test_a_json_quoted_marker_list_is_a_mention():
    text = (
        '{"content": "\\"hospital number\\", \\"nhs number\\", \\"date of birth\\", '
        '\\"clinic letter\\", \\"this is an appraisal\\"}'
        + (" note" * 40)
    )
    assert refusal_for(text, vendor="claude") == ""
    assert refusal_for('{"content": "the hospital number is 12"}', vendor="claude") == ""
    assert refusal_for(
        '{"content": "\\"hospital number\\": \\"0000000\\"}' + (" note" * 40),
        vendor="claude",
    ) == ""


def test_these_turns_are_not_clinic():
    assert refusal_for("in fact running on opus", vendor="claude") == ""
    assert refusal_for("continue it is not clinical", vendor="claude") == ""
    assert refusal_for("and", vendor="claude") == ""


def test_backtick_token_is_a_mention():
    assert refusal_for("`hospital number`", vendor="claude") == ""
