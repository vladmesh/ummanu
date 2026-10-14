"""The one parser of `git worktree list --porcelain [-z]` every worktree reader shares."""

from __future__ import annotations

import unittest

from ummanu.infra import git_worktree

# `git worktree list --porcelain` of a bare main repository with one linked worktree in each shape.
LINES = [
    "worktree /srv/repo.git",
    "bare",
    "",
    "worktree /srv/work/feature",
    "HEAD 5de45712974e6094bbc7507105970408eb69c046",
    "branch refs/heads/feature",
    "",
    "worktree /srv/work/detached",
    "HEAD 9820906a383ef99d9e74bb19f161541fa3151ec0",
    "detached",
    "locked",
    "",
    "worktree /srv/work/held",
    "HEAD 9820906a383ef99d9e74bb19f161541fa3151ec0",
    "branch refs/heads/held",
    "locked on a removable disk",
    "",
    "worktree /srv/work/gone path",
    "HEAD 62921217aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "detached",
    "prunable gitdir file points to non-existent location",
    "",
]

EXPECTED = [
    {"worktree": "/srv/repo.git", "bare": ""},
    {
        "worktree": "/srv/work/feature",
        "HEAD": "5de45712974e6094bbc7507105970408eb69c046",
        "branch": "refs/heads/feature",
    },
    {
        "worktree": "/srv/work/detached",
        "HEAD": "9820906a383ef99d9e74bb19f161541fa3151ec0",
        "detached": "",
        "locked": "",
    },
    {
        "worktree": "/srv/work/held",
        "HEAD": "9820906a383ef99d9e74bb19f161541fa3151ec0",
        "branch": "refs/heads/held",
        "locked": "on a removable disk",
    },
    {
        "worktree": "/srv/work/gone path",
        "HEAD": "62921217aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "detached": "",
        "prunable": "gitdir file points to non-existent location",
    },
]


class ParseListTests(unittest.TestCase):
    def test_newline_listing(self) -> None:
        self.assertEqual(git_worktree.parse_list("\n".join(LINES)), EXPECTED)

    def test_nul_listing(self) -> None:
        self.assertEqual(git_worktree.parse_list("\0".join(LINES)), EXPECTED)

    def test_a_listing_without_the_final_terminator_keeps_its_last_record(self) -> None:
        self.assertEqual(git_worktree.parse_list("\n".join(LINES[:6])), EXPECTED[:2])
        self.assertEqual(git_worktree.parse_list("\0".join(LINES[:6])), EXPECTED[:2])

    def test_a_nul_listing_keeps_a_newline_inside_a_path(self) -> None:
        listing = "worktree /srv/work/odd\nname\0HEAD abc\0detached\0\0"
        self.assertEqual(
            git_worktree.parse_list(listing),
            [{"worktree": "/srv/work/odd\nname", "HEAD": "abc", "detached": ""}],
        )

    def test_nothing_listed(self) -> None:
        self.assertEqual(git_worktree.parse_list(""), [])


if __name__ == "__main__":
    unittest.main()
