from scripts.antibot_eff_finish import (
    EFF_ALLOWED_ORIGINS,
    eff_origin_allowed,
    eff_summary_complete,
)


def test_eff_route_allowlist_is_exactly_the_official_test_graph():
    assert EFF_ALLOWED_ORIGINS == (
        "https://coveryourtracks.eff.org",
        "https://firstpartysimulator.net",
        "https://firstpartysimulator.org",
        "https://trackersimulator.org",
        "https://eviltracker.net",
        "https://do-not-tracker.org",
        "https://media.net",
    )
    assert len(set(EFF_ALLOWED_ORIGINS)) == len(EFF_ALLOWED_ORIGINS)


def test_eff_origin_allowlist_requires_exact_https_origin():
    assert eff_origin_allowed("https://firstpartysimulator.net/kcarter")
    assert not eff_origin_allowed("http://firstpartysimulator.net/kcarter")
    assert not eff_origin_allowed("https://sub.firstpartysimulator.net/kcarter")
    assert not eff_origin_allowed("https://firstpartysimulator.net:8443/kcarter")
    assert not eff_origin_allowed("https://unrelated.example/kcarter")
    assert not eff_origin_allowed("https://user@coveryourtracks.eff.org/results")


def test_eff_completion_requires_result_page_labels_and_all_answer_nodes():
    complete = [True, False, True, True, True, True, True, True, True]
    assert eff_summary_complete("https://coveryourtracks.eff.org/results", complete)
    assert not eff_summary_complete("https://coveryourtracks.eff.org/", complete)
    assert not eff_summary_complete("https://firstpartysimulator.net/results", complete)
    assert not eff_summary_complete("http://coveryourtracks.eff.org/results", complete)
    assert not eff_summary_complete("https://user@coveryourtracks.eff.org/results", complete)
    assert not eff_summary_complete("https://coveryourtracks.eff.org:8443/results", complete)
    assert not eff_summary_complete("https://coveryourtracks.eff.org/results", complete[:8])
    assert not eff_summary_complete("https://coveryourtracks.eff.org/results", [True, True] + [False] * 7)
