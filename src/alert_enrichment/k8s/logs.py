"""Turning noisy container logs into a short, quotable evidence block."""

from __future__ import annotations

import re

from ..utils import strip_ansi

# Deliberately broad: false positives are cheap (an extra line in Slack),
# missed OOM/panic lines are expensive (the whole point of the service).
ERROR_RE = re.compile(
    r"(?i)\b("
    r"error|err|fatal|panic|exception|traceback|stack ?trace|"
    r"oom|out of memory|killed|sigkill|sigterm|segfault|"
    r"timeout|timed out|deadline exceeded|refused|denied|forbidden|"
    r"unavailable|failed|failure|crash|no such host|connection reset|"
    r"could not connect|unable to"
    r")\b"
)

# Stack traces and structured loggers wrap the interesting part onto following
# lines. Keep a short tail after every match, then stop.
CONTINUATION_LINES = 2
CONTINUATION_RE = re.compile(r"^(\s|at\s|Caused by|Traceback|File \"|\.\.\.)")

# Some container runtimes (notably containerd inside kind) answer a
# `--previous` request with a 200 and this message instead of an error.
UNAVAILABLE_LOGS_RE = re.compile(
    r"(?i)^(unable to retrieve container logs|previous terminated container .* not found)"
)


def logs_unavailable(text: str) -> bool:
    sample = text.strip()
    return bool(sample) and bool(UNAVAILABLE_LOGS_RE.search(sample[:400]))


def extract_error_lines(
    text: str,
    *,
    max_lines: int,
    max_chars_per_line: int = 300,
) -> tuple[list[str], bool]:
    """Return (lines, truncated) with the most recent error evidence.

    * strips ANSI colour codes produced by some app loggers;
    * keeps up to ``CONTINUATION_LINES`` following lines (stack traces);
    * collapses repeated identical lines into ``(xN)``;
    * returns the *last* ``max_lines`` so the newest failure wins.
    """

    selected: list[str] = []
    window = 0
    for raw_line in strip_ansi(text).splitlines():
        line = raw_line.strip()
        if ERROR_RE.search(line):
            selected.append(line[:max_chars_per_line])
            window = CONTINUATION_LINES
            continue
        if window > 0 and line and CONTINUATION_RE.match(raw_line):
            selected.append(line[:max_chars_per_line])
            window -= 1
            continue
        if line:
            window = 0

    collapsed: list[str] = []
    counts: list[int] = []
    for line in selected:
        if collapsed and collapsed[-1] == line:
            counts[-1] += 1
        else:
            collapsed.append(line)
            counts.append(1)
    emphasised = [
        f"{line}  (x{count})" if count > 1 else line
        for line, count in zip(collapsed, counts, strict=False)
    ]

    truncated = len(emphasised) > max_lines
    return (emphasised[-max_lines:] if truncated else emphasised), truncated
