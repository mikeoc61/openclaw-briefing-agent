#!/usr/bin/env python3
"""Crypto headlines from CoinDesk + Bitcoin Magazine RSS (max 5)."""
import urllib.request, xml.etree.ElementTree as ET
feeds = [
    'https://www.coindesk.com/arc/outboundfeeds/rss/',
    'https://feeds.feedburner.com/bitcoinmagazine',
]
headers = {'User-Agent': 'Mozilla/5.0'}
MAX = 5
seen = set()
out = []
for feed_url in feeds:
    if len(out) >= MAX:
        break
    try:
        req = urllib.request.Request(feed_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            xml_data = r.read()
        root = ET.fromstring(xml_data)
        for item in root.findall('.//item'):
            if len(out) >= MAX:
                break
            title = (item.findtext('title') or '').strip()
            if not title or title in seen:
                continue
            seen.add(title)
            # link element or guid as fallback permalink
            link = (item.findtext('link') or item.findtext('guid') or '').strip()
            out.append(f'- {title} — {link}' if link else f'- {title}')
    except Exception:
        pass
for line in out:
    print(line)
for _ in range(MAX - len(out)):
    print('- unavailable')
