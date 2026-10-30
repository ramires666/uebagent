from scripts.readiness_diagnostic_outcomes import (
    UNVERIFIED,
    PAGES,
    class_text_outcome,
    diagnostic_verdict,
    known_dependency_blocks,
    languages_presence,
    plugin_length_bucket,
    result_class,
    support_outcome,
    unique_outcome,
)


def test_unique_outcome_requires_one_exact_candidate():
    assert unique_outcome(["passed"], ["passed", "failed"]) == "passed"
    assert unique_outcome(["passed", "failed"], ["passed", "failed"]) == UNVERIFIED
    assert unique_outcome(["maybe"], ["passed", "failed"]) == UNVERIFIED
    assert unique_outcome([], ["passed", "failed"]) == UNVERIFIED


def test_sannysoft_result_classes_require_one_unique_visible_state():
    assert result_class(True, False) == "passed"
    assert result_class(False, True, pass_value="missing (passed)",
                        fail_value="present (failed)") == "present (failed)"
    assert result_class(False, False) == UNVERIFIED
    assert result_class(True, True) == UNVERIFIED


def test_class_must_agree_with_initialized_text_outcome():
    assert class_text_outcome([True, False], [True, False],
                              ("passed", "failed")) == "passed"
    assert class_text_outcome([False, True], [False, True],
                              ("passed", "failed")) == "failed"
    assert class_text_outcome([False, True], [True, False],
                              ("passed", "failed")) == UNVERIFIED
    assert class_text_outcome([False, True], [False, False],
                              ("passed", "failed")) == UNVERIFIED


def test_known_dependency_blocks_only_count_allowlisted_script_origins():
    status = {"blocked_resources": [
        {"origin": "https://cdnjs.cloudflare.com", "resource_type": "script", "count": 4},
        {"origin": "https://cdn.jsdelivr.net", "resource_type": "script", "count": 2},
        {"origin": "https://other.example", "resource_type": "script", "count": 7},
        {"origin": "https://cdnjs.cloudflare.com", "resource_type": "image", "count": 9},
    ]}
    assert known_dependency_blocks(status, PAGES[0]) == {
        "cdnjs.cloudflare.com": 4, "cdn.jsdelivr.net": 2}
    assert known_dependency_blocks(status, PAGES[1]) == {}


def test_plugin_length_is_reduced_to_bucket():
    assert plugin_length_bucket("0") == "zero"
    assert plugin_length_bucket("12") == "nonzero"
    assert plugin_length_bucket("unknown") == UNVERIFIED
    assert plugin_length_bucket(None) == UNVERIFIED


def test_languages_presence_does_not_return_raw_value():
    assert languages_presence("") == "empty"
    assert languages_presence("[]") == "empty"
    assert languages_presence("en-US,en") == "present"
    assert languages_presence(None) == UNVERIFIED


def test_support_boolean_rejects_unknown_and_ambiguous_values():
    assert support_outcome("true") == "true"
    assert support_outcome("false (JavaScript disabled)") == "false"
    assert support_outcome("✔ True ✖ False") == UNVERIFIED
    assert support_outcome("unknown") == UNVERIFIED
    assert support_outcome(None) == UNVERIFIED


def test_diagnostic_status_is_narrow_and_blocker_wins():
    assert diagnostic_verdict(blocked=False, expected_page_verified=True,
                              observations=["true", "zero"]) == "DIAGNOSTIC_COMPLETED"
    assert diagnostic_verdict(blocked=False, expected_page_verified=True,
                              observations=[UNVERIFIED]) == "OBSERVATIONS"
    assert diagnostic_verdict(blocked=True, expected_page_verified=True,
                              observations=["true"]) == "BLOCKED"
