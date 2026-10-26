# tests/selftest.py - offline checks (no Telegram, no network): python -m tests.selftest
from bot.feeds import parse_feed
from bot.state import select_new

XML = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>B new</title><guid>b</guid><link>http://x/b</link><pubDate>Tue, 02 Jan 2024 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description><enclosure url="http://x/b.mp3?a=1" type="audio/mpeg" length="1"/></item>
<item><title>A old</title><guid>a</guid><link>http://x/a</link><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate><enclosure url="http://x/a.mp3" type="audio/mpeg" length="1"/></item>
<item><title>C text</title><guid>c</guid><link>http://x/c</link><pubDate>Wed, 03 Jan 2024 10:00:00 GMT</pubDate></item>
</channel></rss>"""

es = parse_feed(XML)
assert [e.uid for e in es] == ["a", "b", "c"], es            # oldest -> newest
assert es[1].media_kind == "audio" and es[1].summary == "Hello world"
assert es[2].media_kind == "none"
send, seen = select_new(es, [], True, 1, 10)                  # first run: only newest 1, rest marked seen
assert [e.uid for e in send] == ["c"] and seen == ["a", "b"], (send, seen)
send, seen = select_new(es, ["a"], False, 1, 1)               # normal run, cap 1, oldest first
assert [e.uid for e in send] == ["b"]
print("selftest OK")
