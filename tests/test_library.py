"""The path derivation, which everything downstream now trusts.

`tub3/library.py` replaced a fuzzy join — matching the last two or three path components,
lowercased, with ambiguous keys deleted rather than guessed — with a single derived
substitution. That is a much better answer and a much worse failure: when tail matching was
wrong it lost one title, and when a prefix is wrong it loses every one, while looking correct.

So the derivation is tested directly, including the shapes that should refuse to produce one.
Pure functions, no Plex, no share, no box.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tub3 import library as lib                                  # noqa: E402


class DerivePrefix(unittest.TestCase):

    def test_the_real_shape(self):
        """Plex in a container, the share mounted somewhere else entirely."""
        plex = ["/Media/TV", "/Media/Kids TV", "/Media/Movies", "/Media/Kids Movies"]
        box = ["/mnt/tub3/Media/mshare/Kids TV", "/mnt/tub3/Media/mshare/Movies",
               "/mnt/tub3/Media/mshare/TV", "/mnt/tub3/Media/mshare/Kids Movies"]
        self.assertEqual(lib.derive_prefix(plex, box),
                         ("/Media", "/mnt/tub3/Media/mshare"))

    def test_one_library_is_not_enough(self):
        """A single pairing is a coincidence, not a prefix.

        Two libraries agreeing is what makes the common head meaningful. With one, any tail
        that happens to match produces a substitution nothing has corroborated.
        """
        self.assertIsNone(lib.derive_prefix(["/Media/TV"], ["/mnt/share/TV"]))

    def test_nothing_pairs(self):
        self.assertIsNone(lib.derive_prefix(["/data/Shows"], ["/mnt/share/TV", "/mnt/share/Films"]))

    def test_names_are_matched_case_insensitively(self):
        plex = ["/Media/TV Shows", "/Media/MOVIES"]
        box = ["/srv/media/tv shows", "/srv/media/movies"]
        self.assertEqual(lib.derive_prefix(plex, box), ("/Media", "/srv/media"))

    def test_a_library_on_another_volume_refuses_rather_than_returning_nothing(self):
        """The case that would otherwise localise every path to garbage.

        One library somewhere else shortens the common head to the empty string, and an empty
        prefix substitutes into everything and resolves to nothing while looking correct.
        Refusing is the right answer and the caller raises on it.

        It also states a real limitation plainly: a library spread across two volumes cannot
        be described by one substitution, and would need a prefix per library. Nobody has that
        shape here — all four libraries are under /Media on one side and one mount on the
        other — and refusing loudly is better than half-supporting it.
        """
        self.assertIsNone(lib.derive_prefix(
            ["/Media/TV", "/Media/Movies", "/elsewhere/Anime"],
            ["/mnt/share/TV", "/mnt/share/Movies", "/mnt/other/Anime"]))
        self.assertIsNone(lib.derive_prefix(
            ["/Media/TV", "/other/Movies"], ["/mnt/a/TV", "/mnt/b/Movies"]))


class FolderOfRegressions(unittest.TestCase):
    """The three faults that shipped in the first catalogue, each pinned.

    All three passed a build and a verification. That is the point: none of them raised, and
    the check that should have caught the first one filtered it out before looking.
    """

    ROOTS = {"/m/TV", "/m/Kids TV", "/m/Movies", "/m/Kids Movies"}

    def test_a_root_beside_the_real_folder_does_not_win(self):
        """`fill_show_paths` hands back both, and the common head of the pair is the root.

        Fifty series had no folder while their folder was sitting in the list next to it.
        """
        self.assertEqual(
            lib._folder_of(["/m/Kids TV", "/m/Kids TV/Arthur"], self.ROOTS),
            "/m/Kids TV/Arthur")

    def test_a_multi_version_film_never_claims_the_library(self):
        """Two versions loose in a root have the root as their common parent.

        Twelve titles returned it, which is how `/m/Kids Movies` came to mean
        "Alice in Wonderland" — and a channel drawing that source would have played the
        entire children's film library.
        """
        self.assertEqual(
            lib._folder_of(["/m/Kids Movies/Alice in Wonderland.mkv",
                            "/m/Kids Movies/Alice in Wonderland 2010.mkv"], self.ROOTS),
            "")

    def test_a_series_spanning_two_folders_returns_nothing_rather_than_guessing(self):
        """Plex folded `Survivorman and Son` into `Survivorman`, so it spans both.

        There is no one folder to name, and naming the library would be far worse than
        admitting it. The caller uses `paths`.
        """
        self.assertEqual(
            lib._folder_of(["/m/TV/Survivorman",
                            "/m/TV/Survivorman and Son",
                            "/m/TV/Survivorman and Son/Season 1"], self.ROOTS),
            "")

    def test_an_ordinary_series_is_still_its_folder(self):
        self.assertEqual(
            lib._folder_of(["/m/TV/Cheers/Season 1/a.mkv",
                            "/m/TV/Cheers/Season 2/b.mkv"], self.ROOTS),
            "/m/TV/Cheers")


class Localise(unittest.TestCase):

    def test_substitutes_only_the_head(self):
        prefix = ("/Media", "/mnt/tub3/Media/mshare")
        self.assertEqual(
            lib.localise("/Media/TV/How It's Made/Season 1/ep.mkv", prefix),
            "/mnt/tub3/Media/mshare/TV/How It's Made/Season 1/ep.mkv")

    def test_leaves_a_path_it_does_not_recognise_alone(self):
        prefix = ("/Media", "/mnt/share")
        self.assertEqual(lib.localise("/somewhere/else.mkv", prefix), "/somewhere/else.mkv")


class Verify(unittest.TestCase):

    def test_a_prefix_that_resolves(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "TV" / "A Show"
            real.mkdir(parents=True)
            self.assertEqual(lib.verify([str(real)]), [])

    def test_a_prefix_that_does_not(self):
        missing = lib.verify(["/definitely/not/here"])
        self.assertEqual(missing, ["/definitely/not/here"])

    def test_nothing_to_check_is_a_failure_not_a_pass(self):
        """The quiet one. An empty sample must not read as success."""
        self.assertTrue(lib.verify([]))
        self.assertTrue(lib.verify(["", ""]))


class FolderOf(unittest.TestCase):
    """The roots must be passed. Without them a library root looks like an ordinary folder,
    which is precisely the fault that let twelve films claim one."""

    ROOTS = {"/m/TV", "/m/Movies", "/m/Kids Movies"}

    def test_a_series_is_its_folder(self):
        self.assertEqual(
            lib._folder_of(["/m/TV/Show/Season 1/a.mkv", "/m/TV/Show/Season 2/b.mkv"],
                           self.ROOTS),
            "/m/TV/Show")

    def test_a_single_film_loose_in_a_library_root_keeps_its_own_file(self):
        """One file has no ambiguity: name the file, never the library around it."""
        self.assertEqual(lib._folder_of(["/m/Movies/Jaws (1975).mkv"], self.ROOTS),
                         "/m/Movies/Jaws (1975).mkv")

    def test_a_film_in_its_own_folder_names_the_folder(self):
        self.assertEqual(lib._folder_of(["/m/Movies/Jaws (1975)/jaws.mkv"], self.ROOTS),
                         "/m/Movies/Jaws (1975)")


if __name__ == "__main__":
    unittest.main()
