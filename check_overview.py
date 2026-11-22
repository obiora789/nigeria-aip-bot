#!/usr/bin/env python3
"""
check_overview.py — a bare aerodrome name gets the same card everywhere.

  python check_overview.py
      FREE (database reads only). Builds the summary card for every
      aerodrome from the live data and prints, per aerodrome, which of the
      three sections (AD 2.2 / AD 2.12 / AD 2.18) have stored data, and which
      topic buttons will be offered. Use --show DNKN to print one full card.

  python check_overview.py --model
      A few cents. Asks the model, for every aerodrome, the bare name, the
      name + "airport", the ICAO code and "tell me about <name>" (expect the
      card, for the RIGHT aerodrome), and four look-alikes that ask something
      ("<name> tower frequency", "ICAO code for <name>", "<name> runways",
      "what is <ICAO>?"; expect NO card).
"""
import argparse
import asyncio
import sys
from types import SimpleNamespace as N

import resolver
from database import get_charts_smart, get_runway_physical_data, get_structured

BARE = ["{x}", "{x} airport", "{icao}", "tell me about {x}"]
ASKS = ["{x} tower frequency", "ICAO code for {x}", "{x} runways", "what is {icao}?"]


def _name(icao):
    return resolver.AERODROMES[icao][0].title()


def survey(show=None):
    import responder
    import main
    print(f"{'ICAO':5} {'AD 2.2':7} {'AD 2.12':8} {'AD 2.18':8} topics")
    gaps = 0
    for icao in sorted(resolver.AERODROMES):
        a = get_structured(icao, "2.2")
        r = get_runway_physical_data(icao)
        c = get_structured(icao, "2.18")
        card = responder.overview_reply(
            N(label=resolver.aerodrome_full_name(icao) or icao, icao=icao), a, r, c)
        have = [not f"see AD {s}." in card for s in ("2.2", "2.12", "2.18")]
        gaps += have.count(False)
        sent = []

        async def send_message(chat_id, text, reply_markup=None):
            sent.append(reply_markup)
        main.send_message = send_message
        asyncio.run(main._send_overview(0, N(label=icao, icao=icao), "check"))
        topics = [b["text"] for row in sent[-1]["inline_keyboard"] for b in row
                  if b["callback_data"].startswith("ov:")]
        mark = lambda ok: "yes" if ok else "MISSING"
        print(f"{icao:5} {mark(have[0]):7} {mark(have[1]):8} {mark(have[2]):8} {', '.join(topics)}")
        if show and icao == show.upper():
            print("\n" + card + "\n")
    print(f"\n{len(resolver.AERODROMES)} aerodromes; {gaps} section(s) with nothing stored "
          f"(the card says 'Not in my stored copy — see AD 2.x' there).")


def model_check():
    from agent import extract_query_parameters
    bad = total = 0
    for icao in sorted(resolver.AERODROMES):
        x = _name(icao)
        for kind, qs in (("card", BARE), ("ask", ASKS)):
            for q in qs:
                q = q.format(x=x, icao=icao)
                ex = extract_query_parameters(q)
                ov = bool(getattr(ex, "aerodrome_overview", False)) if ex else None
                where = (ex.icao_code if ex and ex.icao_code in resolver.VALID_ICAO
                         else next(iter(resolver.match_name((ex.aerodrome_name or "")
                                                            if ex else "")), None))
                ok = (ov and where == icao) if kind == "card" else not ov
                total += 1
                bad += not ok
                if not ok:
                    print(f"  FAIL  {kind:4} {q!r:40} overview={ov} aerodrome={where}")
    print(f"\n{total - bad}/{total} as expected.")
    return bad


def main_():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--show", help="print the full card for one ICAO")
    args = ap.parse_args()
    resolver.load_index()
    survey(args.show)
    if args.model:
        print()
        sys.exit(1 if model_check() else 0)


if __name__ == "__main__":
    main_()
