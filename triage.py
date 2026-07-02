"""
triage.py — weekly review of what pilots actually asked.

Turns the query log into a short report: the outcome mix and the OPEN review
queue (failures not yet handled), repeated failures first. Mark items handled so
next week only shows new problems — the queue converges instead of re-showing
everything.

    python triage.py                       # open queue, last 7 days
    python triage.py --days 30
    python triage.py --icao DNMM
    python triage.py --all                 # include handled + confident answers
    python triage.py --mark 123 456 --note "fixed chart naming"
"""
import argparse

import observability as obs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--icao", default=None)
    ap.add_argument("--all", action="store_true",
                    help="include handled review items and confident answers")
    ap.add_argument("--mark", nargs="+", metavar="ID",
                    help="mark these log ids reviewed (they leave the queue)")
    ap.add_argument("--note", default=None, help="note to attach when marking")
    args = ap.parse_args()

    if args.mark:
        n = obs.mark_reviewed(args.mark, note=args.note)
        print(f"marked {n} item(s) reviewed.")
        return

    rows = obs.fetch_log(days=args.days, icao=args.icao)
    if not rows:
        print(f"No queries logged in the last {args.days} days.")
        return
    s = obs.summarize(rows)
    total = s["total"]

    print(f"\n=== Vannie triage · last {args.days} days"
          f"{' · ' + args.icao.upper() if args.icao else ''} ===")
    print(f"total: {total} | needs review: {len(s['review'])} "
          f"({100*len(s['review'])//total}%) | open (unhandled): {len(s['open'])}\n")

    print("outcome mix:")
    for path, n in s["paths"].most_common():
        flag = "  <-- review" if path in obs.REVIEW_PATHS else ""
        print(f"  {path:16} {n:5}{flag}")

    if s["clusters"]:
        print("\nopen queue — repeated failures first:")
        for qn, n in s["clusters"].most_common(15):
            ex = next(r for r in s["open"] if obs._norm(r["query"]) == qn)
            print(f"  x{n:<3} #{ex.get('id'):<6} [{ex.get('path'):14}] "
                  f"{ex.get('icao') or '—':6} {qn[:56]}")

    to_show = rows if args.all else s["open"]
    print(f"\n{'all rows' if args.all else 'open queue'} "
          f"({len(to_show)} shown, newest first):")
    for r in to_show[:60]:
        ts = (r.get("created_at") or "")[:16].replace("T", " ")
        sim = f"{r['similarity']:.2f}" if r.get("similarity") is not None else "  — "
        print(f"  #{r.get('id'):<6} {ts} | {r.get('path'):14} | "
              f"{r.get('icao') or '—':6} | sim {sim} | {(r.get('query') or '')[:48]}")
    print("\nmark handled:  python triage.py --mark <id> [<id> ...] --note \"...\"\n")


if __name__ == "__main__":
    main()
