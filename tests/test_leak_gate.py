"""scripts/leak-gate.sh: a word from a private list in a tracked file or a
commit message fails the release, and the gate proves it can fail.

Each test builds a throwaway git repository and a words file outside it, the
way the real list is kept out of the tree.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "scripts" / "leak-gate.sh"
WORDS = "# a comment line, never a pattern: zebraword\n\nzebraword\nnew\\s+thing\n"


@unittest.skipUnless(shutil.which("git") and shutil.which("perl") and shutil.which("bash"), "needs git, perl and bash")
class LeakGateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.words = self.root / "words.txt"
        self.words.write_text(WORDS)
        self.git("init", "-q")
        self.commit({"README": "nothing to see\n"}, "first commit")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout

    def commit(self, files: dict[str, str], message: str) -> None:
        for name, text in files.items():
            (self.repo / name).write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "-m", message)

    def gate(self, *args: str, script: Path = GATE) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(script), *args], cwd=self.repo, capture_output=True, text=True)

    def test_a_clean_tree_and_history_pass(self) -> None:
        run = self.gate(str(self.words))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("leak gate: 0 hits in tracked files, 0 in commit messages (HEAD)", run.stdout)

    def test_a_word_in_a_tracked_file_fails_and_never_prints(self) -> None:
        self.commit({"notes.txt": "line one\nsee ZebraWord here, key=s3cr3t\n"}, "add notes")
        run = self.gate(str(self.words))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("notes.txt:2: [pattern 3]\n", run.stdout)
        self.assertNotIn("zebraword", run.stdout.lower())

    def test_a_hit_never_prints_the_rest_of_its_line(self) -> None:
        """Only the place and the pattern number print: a secret beside the
        word stays out of the terminal and the CI log."""
        self.commit({"notes.txt": "https://zebraword.example/feed?passkey=s3cr3t\n"}, "subject\n\nzebraword passkey=s3cr3t")
        run = self.gate(str(self.words))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertNotIn("s3cr3t", run.stdout)
        self.assertNotIn("passkey", run.stdout)

    def test_an_untracked_file_is_not_the_tree(self) -> None:
        (self.repo / "scratch.txt").write_text("zebraword\n")
        self.assertEqual(self.gate(str(self.words)).returncode, 0)

    def test_a_plain_word_matches_whole_words_only(self) -> None:
        self.commit({"notes.txt": "zebrawords and prezebraword\n"}, "add notes")
        self.assertEqual(self.gate(str(self.words)).returncode, 0)

    def test_an_underscore_ends_a_word(self) -> None:
        """Files and identifiers join words with underscores:
        `<word>_cookies.txt` names the word."""
        self.commit({"notes.txt": "/data/cookies/zebraword_cookies.txt\nmy_zebraword\n"}, "add notes")
        run = self.gate(str(self.words))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("notes.txt:1: [pattern 3]", run.stdout)
        self.assertIn("notes.txt:2: [pattern 3]", run.stdout)

    def test_a_change_from_lower_to_upper_case_ends_a_word(self) -> None:
        self.commit({"notes.txt": "ZebraWordHD\nnewZebraWord\n"}, "add notes")
        run = self.gate(str(self.words))
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("notes.txt:1: [pattern 3]", run.stdout)
        self.assertIn("notes.txt:2: [pattern 3]", run.stdout)

    def test_a_phrase_matches_as_written(self) -> None:
        self.commit({"notes.txt": "a renew  thing\n"}, "add notes")
        run = self.gate(str(self.words))
        self.assertEqual(run.returncode, 1)
        self.assertIn("notes.txt:1: [pattern 4]", run.stdout)

    def test_a_word_in_a_commit_message_fails(self) -> None:
        self.commit({"notes.txt": "clean\n"}, "subject\n\nbody with zebraword in it")
        run = self.gate(str(self.words))
        self.assertEqual(run.returncode, 1)
        sha = self.git("rev-parse", "--short", "HEAD").strip()
        self.assertIn(f"commit {sha}:3: [pattern 3]\n", run.stdout)
        self.assertIn("0 hits in tracked files, 1 in commit messages", run.stdout)

    def test_a_range_limits_the_history_searched(self) -> None:
        self.commit({"notes.txt": "clean\n"}, "zebraword in an old message")
        base = self.git("rev-parse", "HEAD").strip()
        self.commit({"more.txt": "clean\n"}, "a clean message")
        self.assertEqual(self.gate(str(self.words), f"{base}..HEAD").returncode, 0)
        self.assertEqual(self.gate(str(self.words)).returncode, 1)

    def test_a_words_file_with_no_pattern_cannot_pass(self) -> None:
        empty = self.root / "empty.txt"
        empty.write_text("# only a comment\n\n")
        run = self.gate(str(empty))
        self.assertEqual(run.returncode, 2)

    def test_the_self_test_passes_on_a_working_gate(self) -> None:
        run = self.gate("--self-test", str(self.words))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("caught 2 of 2 canaries in a tracked file and 2 of 2 in a commit message", run.stdout)

    def test_the_self_test_fails_on_a_gate_that_lets_words_through(self) -> None:
        """Positive control for the control: a gate that searches the tree
        literally misses every canary, and the self-test says so."""
        broken = self.root / "broken-gate.sh"
        source = GATE.read_text()
        self.assertIn("git grep -P -i", source)
        broken.write_text(source.replace("git grep -P -i", "git grep -F -i"))
        run = self.gate("--self-test", str(self.words), script=broken)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("caught 0 of 2 canaries in a tracked file", run.stdout)
        self.assertIn("self-test: FAIL", run.stdout)


if __name__ == "__main__":
    unittest.main()
