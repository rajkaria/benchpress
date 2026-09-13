"""Read-back comparison: provider mark-up must not turn a faithful write into a mismatch."""

from __future__ import annotations

from benchpress.phases.verify import compare, plain_text


def test_slack_autolinks_are_unwrapped_before_text_comparison() -> None:
    written = "Update: billing@northwindstudio.example -> ap@northwindstudio.example (northwindstudio.example)"
    observed = (
        "Update: <mailto:billing@northwindstudio.example|billing@northwindstudio.example> -> "
        "<mailto:ap@northwindstudio.example|ap@northwindstudio.example> "
        "(<http://northwindstudio.example|northwindstudio.example>)"
    )
    assert plain_text(observed) == written
    assert compare(written, observed, "text")
    assert compare("ap@northwindstudio.example", observed, "contains")


def test_text_comparison_still_fails_on_a_real_difference() -> None:
    assert not compare("moved to ap@x.example", "moved to <mailto:billing@x.example|billing@x.example>", "text")
    assert not compare("anything", None, "text")
