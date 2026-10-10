"""Colour is for a person at a terminal: a pipe, a log or an app reading
rack's output gets plain text, and NO_COLOR is honoured.

    python3 -m unittest discover -s tests
"""
import io
import os
import pty
import subprocess
import sys
import unittest
from unittest import mock

from helpers import BASH, ROOT, FakeMachine, dgx_spark

sys.path.insert(0, os.path.join(ROOT, "py"))
import term  # noqa: E402

HELPERS = '. "%s/lib/common.sh"; bold title; dim note; warn careful' % ROOT


def at_a_terminal(cmd, env):
    """What cmd writes when its stdout and stderr are a pseudo-terminal."""
    master, slave = pty.openpty()
    p = subprocess.Popen(cmd, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    out = b""
    while True:
        try:
            chunk = os.read(master, 65536)
        except OSError:                  # Linux: EIO once the other side is closed
            break
        if not chunk:                    # macOS
            break
        out += chunk
    p.wait(timeout=60)
    os.close(master)
    return out.decode("utf-8", "replace")


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
    env.update(extra)
    return env


class Shell(unittest.TestCase):
    def test_plain_for_a_pipe(self):
        r = subprocess.run([BASH, "-c", HELPERS], capture_output=True, text=True, env=clean_env(), timeout=60)
        self.assertEqual((r.stdout, r.stderr), ("title\nnote\n", "careful\n"))

    def test_colour_at_a_terminal_unless_no_color(self):
        out = at_a_terminal([BASH, "-c", HELPERS], clean_env())
        for seq in ("\x1b[1mtitle\x1b[0m", "\x1b[2mnote\x1b[0m", "\x1b[33mcareful\x1b[0m"):
            self.assertIn(seq, out)
        self.assertNotIn("\x1b", at_a_terminal([BASH, "-c", HELPERS], clean_env(NO_COLOR="1")))

    def test_rack_read_by_an_app_is_plain(self):
        m = FakeMachine()
        try:
            dgx_spark(m)
            m.hostname("box")
            r = m.rack("recipes")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("recipes", r.stdout)
            self.assertNotIn("\x1b", r.stdout + r.stderr)
        finally:
            m.cleanup()


class Python(unittest.TestCase):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    def test_paint(self):
        with mock.patch.dict(os.environ, clean_env(), clear=True):
            self.assertEqual(term.paint("x", "1", io.StringIO()), "x")
            self.assertEqual(term.paint("x", "1", self.Tty()), "\x1b[1mx\x1b[0m")
        with mock.patch.dict(os.environ, clean_env(NO_COLOR="1"), clear=True):
            self.assertEqual(term.paint("x", "1", self.Tty()), "x")


if __name__ == "__main__":
    unittest.main()
