#!/usr/bin/env python3
"""Convert the plain-text brief to minimal HTML for email delivery.

Usage: render_html.py <briefing_plain.txt>
System font stack mimics Gmail plain-text rendering; white-space:pre-wrap
preserves spacing; headline URLs become anchor links.
"""
import sys, re, html
text = open(sys.argv[1]).read()
lines = []
for line in text.split('\n'):
    escaped = html.escape(line)
    # Convert "- Title — https://..." lines to anchor links (hides raw URL)
    m = re.match(r'^(- )(.+?) \u2014 (https?://\S+)$', escaped)
    if m:
        prefix, title, raw_url = m.group(1), m.group(2), m.group(3)
        url = html.unescape(raw_url)
        lines.append(f'{prefix}<a href="{url}">{title}</a>')
    else:
        lines.append(escaped)
body = '\n'.join(lines)
print(f'<html><body><div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;font-size:13px;line-height:1.5;white-space:pre-wrap">{body}</div></body></html>')
