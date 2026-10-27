# scripts/summary.py - turns a workflow log into a short Markdown summary for the GitHub run page.
#   python scripts/summary.py <run|scan|deploy|generic> <logfile> "<title>" >> "$GITHUB_STEP_SUMMARY"
# env JOB_STATUS (success | failure | cancelled) is shown as an icon.
import os
import re
import sys
from pathlib import Path


def lines_of(path: str) -> list[str]:
    try:
        raw = Path(path).read_text("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", l).rstrip() for l in raw]   # drop GitHub timestamps if present


def code(ls: list[str], n: int = 40) -> str:
    body = "\n".join(ls[-n:]) or "(no output)"
    return "<details><summary>Log (last %d lines)</summary>\n\n```\n%s\n```\n</details>" % (min(n, len(ls)), body)


def head(title: str) -> str:
    icon = {"success": "✅", "failure": "❌", "cancelled": "⚪"}.get(os.environ.get("JOB_STATUS", ""), "ℹ️")
    return f"## {icon} {title}\n"


def table(cols: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def run(ls: list[str], title: str) -> str:
    state = next((l for l in ls if l.startswith("state:")), "")
    sites: dict[str, dict] = {}
    cur = None
    notes = []
    for l in ls:
        m = re.match(r"\[(\w+)\] feed=(\d+) to_send=(\d+) first_run=(\w+)", l)
        if m:
            cur = m.group(1)
            sites[cur] = {"feed": m.group(2), "send": m.group(3), "first": m.group(4), "sent": 0, "err": ""}
            continue
        if l.strip().startswith("sent:") and cur:
            sites[cur]["sent"] += 1
        m = re.match(r"\[(\w+)\] (FAILED.*)", l)
        if m:
            sites.setdefault(m.group(1), {"feed": "-", "send": "-", "first": "-", "sent": 0, "err": ""})["err"] = m.group(2)[:120]
        if "topic created" in l:
            notes.append("🆕 " + l)
        if l.startswith("  download failed"):
            notes.append("⚠️ " + l.strip())
    out = [head(title)]
    out.append(("Storage: **D1**" if "D1" in state else "⚠️ Storage: **JSON files** (CF_* secrets missing, D1 is not used)") if state else "")
    if sites:
        out.append(table(["Site", "In feed", "To send", "Sent", "Status"],
                         [[k, v["feed"], v["send"], v["sent"], ("❌ " + v["err"]) if v["err"] else ("first run" if v["first"] == "True" else "ok")]
                          for k, v in sites.items()]))
        if all(v["send"] == "0" and not v["err"] for v in sites.values()):
            out.append("\nNothing new to send: every feed item is already recorded in D1.")
    else:
        out.append("No site lines found - the run probably stopped early.")
    if notes:
        out.append("\n" + "\n".join(f"- {n}" for n in notes[:15]))
    out.append(code(ls))
    return "\n\n".join(x.strip("\n") for x in out if x != "")


def scan(ls: list[str], title: str) -> str:
    rows: dict[str, dict] = {}
    for l in ls:
        m = re.match(r"\[(\w+)\] saved \+(\d+) pages \+(\d+) files", l)
        if m:
            r = rows.setdefault(m.group(1), {"p": 0, "f": 0, "s": "", "d": ""})
            r["p"] += int(m.group(2))
            r["f"] += int(m.group(3))
        m = re.match(r"\[(\w+)\] (complete|partial|blocked): (.*)", l)
        if m:
            r = rows.setdefault(m.group(1), {"p": 0, "f": 0, "s": "", "d": ""})
            r["s"] = {"complete": "✅ complete", "partial": "⏳ partial (continues next run)",
                      "blocked": "🚫 0 pages - site blocks the bot? (retried next run)"}[m.group(2)]
            r["d"] = m.group(3)
        m = re.match(r"\[(\w+)\] (up to date.*|FAILED.*|robots.*|skipped.*|WARNING.*|site refuses.*)", l)
        if m:
            r = rows.setdefault(m.group(1), {"p": 0, "f": 0, "s": "", "d": ""})
            r["s"] = "❌ " + m.group(2)[:100] if m.group(2).startswith("FAILED") else m.group(2)[:100]
    out = [head(title)]
    if rows:
        out.append(table(["Site", "New pages", "New files", "Status", "Totals this run"],
                         [[k, v["p"], v["f"], v["s"] or "?", v["d"]] for k, v in rows.items()]))
        out.append("\nOnly pages that carry wanted files are stored. Check in Telegram: /catalog")
    else:
        out.append("No site lines found - the scan probably stopped early.")
    out.append("\n" + code(ls))
    return "\n".join(out)


def deploy(ls: list[str], title: str) -> str:
    out = [head(title)]
    url = next((l.split(":", 1)[1].strip() for l in ls if l.startswith("worker url:")), "")
    out.append(f"Worker: `{url}`" if url else "Worker URL: not found in log")
    api = [l for l in ls if l.startswith("{") and '"ok"' in l]
    names = ["setWebhook", "setMyCommands", "setChatMenuButton"]
    rows = []
    for i, l in enumerate(api):
        ok = '"ok":true' in l.replace(" ", "")
        d = re.search(r'"description":"([^"]*)"', l)
        rows.append([names[i] if i < len(names) else f"call {i + 1}", "✅" if ok else "❌", d.group(1) if d else ""])
    if rows:
        out.append("\n" + table(["Telegram call", "OK", "Note"], rows))
    out.append("\n" + code(ls))
    return "\n".join(out)


def generic(ls: list[str], title: str) -> str:
    return head(title) + "\n" + code(ls, 60)


if __name__ == "__main__":
    kind, path, title = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "Summary"
    print({"run": run, "scan": scan, "deploy": deploy}.get(kind, generic)(lines_of(path), title))
