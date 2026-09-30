#!/usr/bin/env python3
"""claude-usage: plan-limit monitor and per-project budgets for Claude Code.

Data sources
  * snapshots.jsonl - real plan-limit percentages (5h / 7d windows) appended by
    ~/.claude/statusline.sh from the `rate_limits` field Claude Code passes to it.
  * ~/.claude/projects/**/*.jsonl - session transcripts with per-message token
    usage; converted to API-price "cost units" to estimate each project's share.

Per-project % is an estimate: project_cost / all_local_cost * weekly_used_pct.
Usage outside Claude Code on this Mac (claude.ai, other machines) is invisible
here, so project shares are over- rather than under-estimated.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".claude" / "usage-monitor"
SNAPSHOTS = BASE / "snapshots.jsonl"
BUDGETS = BASE / "budgets.json"
CACHE = BASE / "transcripts-cache.json"
PROJECTS = HOME / ".claude" / "projects"

WEEK = 7 * 86400
FIVE_H = 5 * 3600
CACHE_VERSION = 1
KEEP_RECORDS_S = 9 * 86400

# USD per 1M tokens: (input, output, cache_read). Cache writes: 1.25x input (5m), 2x input (1h).
# Only the ratios between models matter; limits are assumed to scale with API price.
PRICES = [
    ("claude-fable-5-1", (10.0, 50.0, 0.25)),
    ("claude-mythos-5-1", (10.0, 50.0, 0.25)),
    ("claude-fable-5", (10.0, 50.0, 1.0)),
    ("claude-mythos", (10.0, 50.0, 1.0)),
    ("claude-opus-5-5", (4.0, 20.0, 0.20)),
    ("claude-opus-5", (5.0, 25.0, 0.5)),
    ("claude-opus-4-8", (5.0, 25.0, 0.5)),
    ("claude-opus-4-7", (5.0, 25.0, 0.5)),
    ("claude-opus-4-6", (5.0, 25.0, 0.5)),
    ("claude-opus-4-5", (5.0, 25.0, 0.5)),
    ("claude-opus-4", (15.0, 75.0, 1.5)),
    ("claude-sonnet-5", (2.0, 10.0, 0.2)),
    ("claude-sonnet-4", (3.0, 15.0, 0.3)),
    ("claude-haiku-4", (1.0, 5.0, 0.1)),
    ("claude-haiku", (0.8, 4.0, 0.08)),
]
DEFAULT_PRICE = (5.0, 25.0, 0.5)


def price_for(model):
    for prefix, p in PRICES:
        if model.startswith(prefix):
            return p
    return DEFAULT_PRICE


def cost_of(rec):
    _, _, model, _, inp, out, cw5, cw1h, cr, fast = rec
    pi, po, pr = price_for(model)
    usd = (inp * pi + out * po + cw5 * pi * 1.25 + cw1h * pi * 2.0 + cr * pr) / 1e6
    return usd * (2.0 if fast else 1.0)


# ---------------------------------------------------------------- transcripts

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def scan_transcripts(max_age_s=KEEP_RECORDS_S):
    """Incrementally parse transcripts; returns list of records (deduped by message id)."""
    now = time.time()
    try:
        cache = json.loads(CACHE.read_text())
        if cache.get("v") != CACHE_VERSION:
            raise ValueError
    except Exception:
        cache = {"v": CACHE_VERSION, "files": {}}
    files = cache["files"]
    seen_paths = set()
    for path in PROJECTS.rglob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        if now - st.st_mtime > max_age_s:
            continue
        key = str(path)
        seen_paths.add(key)
        entry = files.get(key)
        if not entry or entry["ino"] != st.st_ino or entry["off"] > st.st_size:
            entry = {"ino": st.st_ino, "off": 0, "recs": {}}
        if st.st_size > entry["off"]:
            with open(path, "rb") as f:
                f.seek(entry["off"])
                chunk = f.read()
            end = chunk.rfind(b"\n")
            if end >= 0:
                for line in chunk[: end + 1].splitlines():
                    if b'"usage"' not in line or b'"assistant"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    model = msg.get("model") or ""
                    if d.get("type") != "assistant" or not u or model.startswith("<"):
                        continue
                    cc = u.get("cache_creation") or {}
                    cw1h = cc.get("ephemeral_1h_input_tokens")
                    cw_total = u.get("cache_creation_input_tokens") or 0
                    if cw1h is None:
                        cw1h, cw5 = 0, cw_total
                    else:
                        cw5 = cw_total - cw1h
                    rid = f'{msg.get("id")}/{d.get("requestId")}'
                    entry["recs"][rid] = [
                        parse_ts(d["timestamp"]),
                        d.get("cwd") or "",
                        model,
                        d.get("sessionId") or "",
                        u.get("input_tokens") or 0,
                        u.get("output_tokens") or 0,
                        cw5,
                        cw1h,
                        u.get("cache_read_input_tokens") or 0,
                        u.get("speed") == "fast",
                    ]
                entry["off"] += end + 1
        files[key] = entry
    # drop stale files and old records
    for key in list(files):
        if key not in seen_paths:
            del files[key]
            continue
        recs = files[key]["recs"]
        for rid in [r for r, v in recs.items() if now - v[0] > max_age_s]:
            del recs[rid]
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, separators=(",", ":")))
    tmp.replace(CACHE)
    merged = {}
    for entry in files.values():
        merged.update(entry["recs"])
    return sorted(merged.values(), key=lambda r: r[0])


# ------------------------------------------------------------------ snapshots

def load_snapshots():
    out = []
    try:
        with open(SNAPSHOTS) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return [s for s in out if s.get("seven_day_pct") is not None or s.get("five_hour_pct") is not None]


def pct_at(snaps, key, reset_key, t, reset_at):
    """Latest snapshot value of `key` at or before t that belongs to the same window."""
    val = None
    for s in snaps:
        if s["ts"] > t:
            break
        if s.get(key) is not None and abs((s.get(reset_key) or 0) - reset_at) < 120:
            val = s[key]
    return val


# ------------------------------------------------------------------- helpers

WORKTREE_RE = re.compile(r"/\.(?:claude|git)/worktrees/.*$")


def project_root(path):
    # Claude Code worktrees live under <repo>/.claude/worktrees/<name>
    p = WORKTREE_RE.sub("", os.path.abspath(path))
    try:
        r = subprocess.run(["git", "-C", p, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            common = r.stdout.strip()
            if common.endswith("/.git"):
                return common[:-5]
        r = subprocess.run(["git", "-C", p, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return p


def in_project(cwd, root):
    return cwd == root or cwd.startswith(root.rstrip("/") + "/")


def parse_since(s, now):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([mhdw])", s)
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400, "w": WEEK}[m.group(2)]
        return now - float(m.group(1)) * mult
    return datetime.fromisoformat(s).astimezone().timestamp()


def fmt_t(ts):
    return datetime.fromtimestamp(ts).strftime("%a %d.%m %H:%M")


def fmt_dur(s):
    s = int(max(s, 0))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return (f"{d}д " if d else "") + (f"{h}ч " if h or d else "") + f"{m}м"


def load_budgets():
    try:
        return json.loads(BUDGETS.read_text())
    except Exception:
        return {}


def short(root):
    return root.replace(str(HOME), "~")


# -------------------------------------------------------------------- report

def build(args):
    now = time.time()
    root = project_root(args.project)
    snaps = load_snapshots()
    recs = scan_transcripts()
    last = snaps[-1] if snaps else None

    res = {"now": now, "project": root, "limits": None, "windows": {}, "project_stats": {},
           "top_projects": [], "since": None, "budget": None, "warnings": []}

    if not last:
        res["warnings"].append("Нет снимков лимитов: statusline ещё не записал rate_limits "
                               "(нужна подписка Pro/Max и хотя бы один ответ модели после установки).")
    for win, key, rkey, length in (("seven_day", "seven_day_pct", "seven_day_resets_at", WEEK),
                                   ("five_hour", "five_hour_pct", "five_hour_resets_at", FIVE_H)):
        snap = next((s for s in reversed(snaps) if s.get(key) is not None), None)
        if not snap:
            continue
        reset_at = snap[rkey]
        start = reset_at - length
        if reset_at <= now:
            # window rolled over since the last snapshot; nothing measured yet
            res["windows"][win] = {"used_pct": 0.0, "estimated": True, "resets_at": None,
                                   "note": "окно сброшено, свежего снимка ещё нет"}
            continue
        wrecs = [r for r in recs if start <= r[0] <= now]
        total_cost = sum(cost_of(r) for r in wrecs)
        cost_to_snap = sum(cost_of(r) for r in wrecs if r[0] <= snap["ts"])
        pct_per_usd = snap[key] / cost_to_snap if cost_to_snap > 0 else None
        used = snap[key]
        estimated = False
        if pct_per_usd:
            extra = total_cost - cost_to_snap
            if extra > 0:
                used = snap[key] + extra * pct_per_usd
                estimated = True
        proj_cost = sum(cost_of(r) for r in wrecs if in_project(r[1], root))
        w = {
            "used_pct": round(used, 2),
            "measured_pct": snap[key],
            "measured_at": snap["ts"],
            "estimated": estimated,
            "resets_at": reset_at,
            "window_start": start,
            "local_cost_usd_eq": round(total_cost, 2),
            "pct_per_usd_eq": pct_per_usd,
            "project_cost_usd_eq": round(proj_cost, 2),
            "project_pct": round(proj_cost * pct_per_usd, 2) if pct_per_usd else None,
        }
        # burn rate over the last hour, measured from snapshots when possible
        h_ago = pct_at(snaps, key, rkey, now - 3600, reset_at)
        if h_ago is not None:
            w["last_hour_pct"] = round(used - h_ago, 2)
        elif pct_per_usd:
            w["last_hour_pct"] = round(sum(cost_of(r) for r in wrecs if r[0] >= now - 3600) * pct_per_usd, 2)
        rate = w.get("last_hour_pct")
        if rate and rate > 0 and now + (100 - used) / rate * 3600 < reset_at:
            w["hours_to_100_at_last_hour_rate"] = round((100 - used) / rate, 1)
        res["windows"][win] = w

    wk = res["windows"].get("seven_day")
    if wk and wk.get("pct_per_usd_eq"):
        by_proj = {}
        for r in recs:
            if wk["window_start"] <= r[0] <= now:
                by_proj.setdefault(r[1], 0.0)
                by_proj[r[1]] += cost_of(r)
        # group cwd -> git root (cache per cwd)
        roots = {}
        grouped = {}
        for cwd, c in by_proj.items():
            if cwd not in roots:
                roots[cwd] = project_root(cwd) if os.path.isdir(cwd) else WORKTREE_RE.sub("", cwd)
            grouped[roots[cwd]] = grouped.get(roots[cwd], 0.0) + c
        res["top_projects"] = sorted(
            ({"project": p, "cost_usd_eq": round(c, 2), "pct_of_week": round(c * wk["pct_per_usd_eq"], 2)}
             for p, c in grouped.items()), key=lambda x: -x["cost_usd_eq"])[: args.top]

    # project stats over fixed periods
    pp = wk.get("pct_per_usd_eq") if wk else None
    for label, secs in (("1h", 3600), ("5h", FIVE_H), ("24h", 86400), ("7d", WEEK)):
        prs = [r for r in recs if r[0] >= now - secs and in_project(r[1], root)]
        c = sum(cost_of(r) for r in prs)
        res["project_stats"][label] = {
            "cost_usd_eq": round(c, 2),
            "pct_of_week": round(c * pp, 2) if pp else None,
            "output_tokens": sum(r[5] for r in prs),
            "messages": len(prs),
            "sessions": len({r[3] for r in prs}),
        }

    if args.since:
        t0 = parse_since(args.since, now)
        prs = [r for r in recs if r[0] >= t0 and in_project(r[1], root)]
        allr = [r for r in recs if r[0] >= t0]
        c, ca = sum(cost_of(r) for r in prs), sum(cost_of(r) for r in allr)
        res["since"] = {"from": t0, "project_cost_usd_eq": round(c, 2),
                        "project_pct_of_week": round(c * pp, 2) if pp else None,
                        "all_pct_of_week": round(ca * pp, 2) if pp else None}

    b = load_budgets().get(root)
    if b and wk and wk.get("project_pct") is not None:
        used = wk["project_pct"]
        res["budget"] = {"weekly_pct": b["weekly_pct"], "used_pct": used,
                         "remaining_pct": round(b["weekly_pct"] - used, 2),
                         "exceeded": used >= b["weekly_pct"]}
    elif b:
        res["budget"] = {"weekly_pct": b["weekly_pct"], "used_pct": None, "remaining_pct": None,
                         "exceeded": False}

    if last and now - last["ts"] > 3 * 3600:
        res["warnings"].append(f"Последний снимок лимитов {fmt_dur(now - last['ts'])} назад; "
                               "значения экстраполированы по токенам.")
    return res


def print_report(res):
    root = res["project"]
    print(f"Проект: {short(root)}")
    for w in res["warnings"]:
        print(f"! {w}")
    names = {"five_hour": "5-часовой", "seven_day": "Недельный"}
    for win in ("five_hour", "seven_day"):
        w = res["windows"].get(win)
        if not w:
            continue
        if w.get("resets_at") is None:
            print(f"{names[win]:>10}: 0% ({w['note']})")
            continue
        est = " ~" if w["estimated"] else " "
        line = (f"{names[win]:>10}:{est}{w['used_pct']:.1f}% использовано, сброс {fmt_t(w['resets_at'])} "
                f"(через {fmt_dur(w['resets_at'] - res['now'])})")
        if w.get("last_hour_pct") is not None:
            line += f", за час +{w['last_hour_pct']:.1f}%"
        if w.get("hours_to_100_at_last_hour_rate"):
            line += f", при таком темпе 100% через {w['hours_to_100_at_last_hour_rate']}ч"
        print(line)
        if w.get("project_pct") is not None:
            print(f"{'':>12}этот проект в текущем окне: ~{w['project_pct']:.1f}%")
    ps = res["project_stats"]
    print("\nЭтот проект (доля недельного лимита, оценка):")
    for label in ("1h", "5h", "24h", "7d"):
        s = ps[label]
        pct = f"{s['pct_of_week']:.2f}%" if s["pct_of_week"] is not None else "n/a"
        print(f"  {label:>4}: {pct:>8}  ${s['cost_usd_eq']:>8.2f} экв.  "
              f"{s['messages']:>5} ответов  {s['sessions']} сессий")
    if res["since"]:
        s = res["since"]
        print(f"\nС {fmt_t(s['from'])}: проект ~{s['project_pct_of_week']}% недели, "
              f"всего ~{s['all_pct_of_week']}%")
    if res["top_projects"]:
        print("\nПроекты в текущем недельном окне:")
        for p in res["top_projects"]:
            print(f"  {p['pct_of_week']:>6.1f}%  ${p['cost_usd_eq']:>8.2f}  {short(p['project'])}")
    b = res["budget"]
    if b:
        if b["used_pct"] is None:
            print(f"\nБюджет: {b['weekly_pct']}% недели (расход пока не посчитан)")
        else:
            state = "ПРЕВЫШЕН" if b["exceeded"] else f"осталось {b['remaining_pct']:.1f}%"
            print(f"\nБюджет: {b['used_pct']:.1f}% из {b['weekly_pct']}% недели — {state}")


def cmd_report(args):
    res = build(args)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_report(res)


def cmd_check(args):
    """Exit 0 = within budget, 2 = exceeded, 3 = no budget/data. One-line output for agents."""
    res = build(args)
    b = res["budget"]
    wk = res["windows"].get("seven_day") or {}
    fh = res["windows"].get("five_hour") or {}
    head = (f"week {wk.get('used_pct', '?')}% | 5h {fh.get('used_pct', '?')}% | "
            f"project ~{wk.get('project_pct', '?')}% of week")
    if not b:
        print(head + " | no budget set")
        sys.exit(3)
    if b["used_pct"] is None:
        print(head + f" | budget {b['weekly_pct']}% (no data yet)")
        sys.exit(3)
    if b["exceeded"]:
        print(head + f" | BUDGET EXCEEDED: {b['used_pct']:.1f}% of {b['weekly_pct']}%")
        sys.exit(2)
    print(head + f" | budget {b['used_pct']:.1f}/{b['weekly_pct']}%, remaining {b['remaining_pct']:.1f}%")


def cmd_budget(args):
    root = project_root(args.project)
    budgets = load_budgets()
    if args.action == "set":
        budgets[root] = {"weekly_pct": float(args.pct), "set_at": time.time()}
    elif args.action == "clear":
        budgets.pop(root, None)
    else:
        for p, b in budgets.items():
            print(f"{b['weekly_pct']:>6}%  {short(p)}")
        return
    BASE.mkdir(parents=True, exist_ok=True)
    BUDGETS.write_text(json.dumps(budgets, ensure_ascii=False, indent=2))
    print(f"{short(root)}: " + (f"бюджет {args.pct}% недели" if args.action == "set" else "бюджет снят"))


def cmd_hook(args):
    """UserPromptSubmit hook: inject a one-line budget status when the project has a budget."""
    try:
        data = json.load(sys.stdin)
        args.project = data.get("cwd") or args.project
    except Exception:
        pass
    if project_root(args.project) not in load_budgets():
        return
    try:
        res = build(args)
    except Exception:
        return
    b, wk = res["budget"], res["windows"].get("seven_day") or {}
    if not b or b["used_pct"] is None:
        return
    msg = (f"[claude-usage] Бюджет проекта: {b['used_pct']:.1f}% из {b['weekly_pct']}% недельного лимита "
           f"(неделя всего {wk.get('used_pct')}%).")
    if b["exceeded"]:
        msg += " Бюджет превышен: не начинай новую тяжёлую работу, сообщи пользователю."
    elif b["remaining_pct"] < 0.2 * b["weekly_pct"]:
        msg += " Осталось меньше 20% бюджета: экономь (без лишних субагентов)."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": msg}}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(prog="claude-usage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--project", default=os.getcwd(), help="project dir (default: cwd, resolved to git root)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("report", help="full report (default)")
    r.add_argument("--json", action="store_true")
    r.add_argument("--since", help="also count usage since: 30m, 2h, 1d or ISO time")
    r.add_argument("--top", type=int, default=8)
    sub.add_parser("check", help="one-line status; exit 2 if project budget exceeded")
    b = sub.add_parser("budget", help="set/clear/list weekly budget for the project")
    b.add_argument("action", choices=["set", "clear", "list"])
    b.add_argument("pct", nargs="?", help="percent of the weekly limit, e.g. 10")
    sub.add_parser("hook", help="UserPromptSubmit hook entry point")
    args = ap.parse_args()
    for k, v in (("json", False), ("since", None), ("top", 8)):
        if not hasattr(args, k):
            setattr(args, k, v)
    if args.cmd == "budget":
        if args.action == "set" and not args.pct:
            ap.error("budget set needs a percent")
        cmd_budget(args)
    elif args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "hook":
        cmd_hook(args)
    else:
        cmd_report(args)


if __name__ == "__main__":
    main()
