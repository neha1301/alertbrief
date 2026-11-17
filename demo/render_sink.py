#!/usr/bin/env python3
"""Render sink JSONL as a Slack-like message in the terminal and (optionally) HTML.

Used for the README screenshot when no Slack workspace is wired up. The HTML is
a faithful, self-contained preview of the same Block Kit payload that
``SlackPublisher`` posts.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any

HTML_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Slack preview — alert-enrichment</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; padding: 32px 16px; background: #f4f4f6;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  .msg { max-width: 760px; margin: 0 auto 24px; background: #fff; border: 1px solid #ddd;
         border-radius: 12px; padding: 16px 20px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .msg p { margin: 8px 0; font-size: 14px; line-height: 1.45; color: #1d1c1d; }
  .header { font-size: 18px; font-weight: 700; margin: 4px 0 8px; }
  .context { font-size: 12px; color: #616061; margin: 6px 0; }
  .fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 24px; margin: 12px 0; }
  .field b { display: block; font-size: 12px; color: #616061; text-transform: none; }
  .field span { font-size: 14px; white-space: pre-wrap; }
  pre { background: #f8f8f8; border: 1px solid #e8e8e8; border-radius: 6px;
        padding: 12px; font-size: 12px; line-height: 1.5; overflow-x: auto; }
  code { background: #f8f8f8; border: 1px solid #e8e8e8; border-radius: 3px;
         padding: 1px 4px; font-size: 12px; }
  .actions { margin: 12px 0 4px; display: flex; gap: 8px; }
  .btn { display: inline-block; padding: 6px 12px; border: 1px solid #1d1c1d33;
         border-radius: 4px; font-size: 13px; font-weight: 600; color: #1d1c1d;
         text-decoration: none; }
  .btn.danger { color: #e01e5a; border-color: #e01e5a66; }
  .divider { border-top: 1px solid #eee; margin: 12px 0; }
</style>
</head>
<body>
"""

HTML_TAIL = """</body>
</html>
"""


def mrkdwn_to_html(text: str) -> str:
    parts = text.split("```")
    rendered: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            rendered.append(f"<pre>{html.escape(part.strip(chr(10)))}</pre>")
            continue
        escaped = html.escape(part)
        escaped = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
        rendered.append(escaped.replace("\n", "<br>"))
    return "".join(rendered)


def terminal_lines(body: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for block in body.get("blocks", []):
        kind = block.get("type")
        if kind == "header":
            title = block["text"]["text"]
            lines.extend(["", title, "─" * min(len(title) + 4, 78)])
        elif kind == "context":
            lines.append(" · ".join(element.get("text", "") for element in block["elements"]))
        elif kind == "section":
            if "text" in block:
                lines.append(block["text"]["text"].replace("```", "").strip())
            for field in block.get("fields", []):
                label, _, value = field["text"].partition("\n")
                lines.append(f"{label.strip('*'):<14} {value}")
        elif kind == "actions":
            buttons = "   ".join(
                f"[{element['text']['text']}]" for element in block["elements"]
            )
            lines.append(buttons)
        elif kind == "divider":
            lines.append("─" * 60)
    return lines


def card_html(body: dict[str, Any]) -> str:
    rendered: list[str] = ['<article class="msg">']
    for block in body.get("blocks", []):
        kind = block.get("type")
        if kind == "header":
            rendered.append(f'<div class="header">{html.escape(block["text"]["text"])}</div>')
        elif kind == "context":
            text = " · ".join(element.get("text", "") for element in block["elements"])
            rendered.append(f'<div class="context">{mrkdwn_to_html(text)}</div>')
        elif kind == "section":
            if "text" in block:
                rendered.append(f"<p>{mrkdwn_to_html(block['text']['text'])}</p>")
            if "fields" in block:
                fields = "".join(
                    f'<div class="field">{mrkdwn_to_html(field["text"])}</div>'
                    for field in block["fields"]
                )
                rendered.append(f'<div class="fields">{fields}</div>')
        elif kind == "actions":
            buttons = "".join(
                f'<a class="btn{" danger" if element.get("style") == "danger" else ""}" '
                f'href="{html.escape(element.get("url", "#"))}">{html.escape(element["text"]["text"])}</a>'
                for element in block["elements"]
            )
            rendered.append(f'<div class="actions">{buttons}</div>')
        elif kind == "divider":
            rendered.append('<div class="divider"></div>')
    rendered.append("</article>")
    return "".join(rendered)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sink", type=Path, help="JSONL file written by SinkPublisher")
    parser.add_argument("--html", type=Path, default=None, help="also write a Slack-like HTML preview")
    args = parser.parse_args()

    if not args.sink.exists():
        print(f"no sink file at {args.sink}; run the demo first")
        return 1

    bodies = [json.loads(line) for line in args.sink.read_text(encoding="utf-8").splitlines() if line]
    for index, body in enumerate(bodies, start=1):
        print(f"\n=== Slack message {index}/{len(bodies)} ===")
        print("\n".join(terminal_lines(body)))

    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(
            HTML_HEAD + "".join(card_html(body) for body in bodies) + HTML_TAIL,
            encoding="utf-8",
        )
        print(f"\nHTML preview: {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
