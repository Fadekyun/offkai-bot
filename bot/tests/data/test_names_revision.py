"""Cross-language names-revision vectors shared with the Next.js helper."""

import pytest

from offkai_bot.data.response import compute_names_revision


@pytest.mark.parametrize(
    "display_name,extra_people,extras_names,expected",
    [
        (None, 0, [], "6bc5c3cd0b0c09ef7f909772f36e25e70b7fc42e7b9bfddeee2796e5fef7211e"),
        ("", 0, [], "c418343ea53bb9cd9cd8e7d2ed3ee78c96d0f96c899661be0b2cf2cc196cd5db"),
        ("Before", 2, ["Alice", "Bob"], "7c709e8979b4d4ec70d3cccf92484a282ae394b60e828a9d37a2d802453bdbef"),
        (
            "日本語😀",
            5,
            ["客😀", None, "", "surplus😀"],
            "bb4b4035c05c012473db134ae01e38391b2a51fb78c79270737300da96a3f333",
        ),
        (
            "\u007f\u0080\u2028\ud83d\ude00",
            1,
            ["\u0000\t\n"],
            "6a53ee2393228323544afecfe0189593d565f6fd437d2dabc3955036b3282218",
        ),
    ],
)
def test_revision_vectors_match_the_browser(display_name, extra_people, extras_names, expected):
    assert compute_names_revision(display_name, extra_people, extras_names) == expected


def test_missing_and_surplus_guest_slots_are_distinct():
    assert compute_names_revision(None, 2, ["Alice"]) != compute_names_revision(None, 2, ["Alice", ""])
    assert compute_names_revision(None, 2, ["Alice", "Bob"]) != compute_names_revision(
        None, 2, ["Alice", "Bob", "Legacy surplus"]
    )
