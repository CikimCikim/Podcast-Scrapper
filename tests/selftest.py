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

# --- caption / extras / stores
import asyncio
import json

from bot.extras import caption, find_extras, page_candidates, title_matches
from bot.feeds import Entry

e = Entry(uid="u1", title="A <b> & title", link="http://www.bbc.co.uk/programmes/p0phz9z7", summary="x" * 3000,
          media_url="http://x/a.mp3", media_kind="audio", published=1793000000.0)
c = caption(e)
assert '<a href="http://www.bbc.co.uk/programmes/p0phz9z7">26 October 2026</a>' in c, c
assert "A &lt;b&gt; &amp; title" in c and len(c) <= 1024, len(c)
page = '<a href="/x/ep-1_transcript.pdf">t</a> <a href=\'https://d.bbc.co.uk/w.pdf?v=1\'>w</a> <a href="/x/ep-1_transcript.pdf">dup</a><a href="/a.html">n</a>'
assert find_extras(page, "https://www.bbc.co.uk/learningenglish/ep-1") == [
    "https://www.bbc.co.uk/x/ep-1_transcript.pdf", "https://d.bbc.co.uk/w.pdf?v=1"]

assert page_candidates("https://b/{yyyy}/ep-{yymmdd}", 1793000000.0)[:3] == ["https://b/2026/ep-261026", "https://b/2026/ep-261025", "https://b/2026/ep-261027"]
assert title_matches("Why has gold got so expensive?", "<h1>Why has gold got so expensive</h1><p>x</p>")
assert not title_matches("Why has gold got so expensive?", "<h1>Learn about grammar</h1>")
js = r'{"pdf":"https:\/\/downloads.bbc.co.uk\/le\/261026_6min_transcript.pdf","o":"https:\/\/downloads.bbc.co.uk\/le\/terms.pdf"}'
assert find_extras(js, "https://x/") == ["https://downloads.bbc.co.uk/le/261026_6min_transcript.pdf", "https://downloads.bbc.co.uk/le/terms.pdf"]
assert find_extras(js, "https://x/", "261026") == ["https://downloads.bbc.co.uk/le/261026_6min_transcript.pdf"]
assert 'Lesson page</a>' in caption(e, "https://bbc/lesson") and len(caption(e, "https://bbc/lesson")) <= 1024
try:
    import httpx
    from bot.store import D1Store
except ImportError:          # httpx is installed in Actions; skip the D1 mock test where it is missing
    httpx = None
    print("selftest2 OK (D1 mock test skipped: httpx not installed)")
if httpx:
    calls = []


    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body)
        rows = [{"uid": "old"}] if body["sql"].startswith("SELECT uid") else []
        return httpx.Response(200, json={"success": True, "result": [{"results": rows}]})


    async def d1_test():
        s = D1Store("acc", "db", "tok", transport=httpx.MockTransport(handler))
        assert await s.seen_uids("bbc") == (["old"], False)
        await s.mark_skipped("bbc", [Entry(str(i), "t", "", "", None, "none", published=1.0) for i in range(45)])
        assert [len(c["params"]) for c in calls[1:]] == [80, 80, 20]      # 20 rows per statement (<100 params)
        await s.record_sent("bbc", e, ["u"], 5, 6, [7])
        await s.set_topic("bbc", 7, "BBC")
        await s.flush()

    asyncio.run(d1_test())
    print("selftest2 OK")
