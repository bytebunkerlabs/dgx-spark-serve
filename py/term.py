"""Colour for a person at a terminal; plain text for a pipe, a log or an app
reading the output, and for anyone who sets NO_COLOR."""
import os
import sys


def paint(text, code, stream=None):
    stream = stream if stream is not None else sys.stdout
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False
    if not tty or os.environ.get("NO_COLOR"):
        return str(text)
    return "\033[%sm%s\033[0m" % (code, text)
