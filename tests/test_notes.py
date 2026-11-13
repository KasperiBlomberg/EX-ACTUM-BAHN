"""The note reader's cases, the same as tests/notes.test.mjs. Run: python tests/test_notes.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spread_bot.notes import parse_note  # noqa: E402

D = 1440
MON10, TUE10 = 600, D + 600


def hm(h, m=0):
    return h * 60 + m


def dhm(d, h, m=0):
    return d * D + h * 60 + m


CASES_1 = [
    ("Nouto arkisin ennen klo 14.", MON10, lambda f: f["pickup_close"] == hm(14)),
    ("Klar först på onsdag kl 12", MON10, lambda f: f["ready_at"] == 2 * D + hm(12)),
    ("Maksun jälkeen, ei ennen klo 13.30.", MON10, lambda f: f["release_at"] == hm(13, 30)),
    ("Must be at destination by tomorrow 17:00.", MON10, lambda f: f["deadline_at"] == D + hm(17)),
    ("Vastaanotto klo 16 asti.", MON10, lambda f: f["delivery_close"] == hm(16)),
    ("Manuaalivaihteisto.", MON10, lambda f: f["manual"]),
    ("Car has no plates.", MON10, lambda f: f["plates"]),
    ("Auto vielä kesärenkailla.", MON10, lambda f: f["summer"]),
    ("Ready at two", MON10, lambda f: f["ready_at"] == hm(14)),
    ("Valmis puoli kolme", MON10, lambda f: f["ready_at"] == hm(14, 30)),
    ("Klar halv tre", MON10, lambda f: f["ready_at"] == hm(14, 30)),
    ("Ready tomorrow at 3pm, not 3pm sorry, 4pm", MON10, lambda f: f["ready_at"] == D + hm(16)),
    ("Valmis klo 12. Korjaus: klo 13.", MON10, lambda f: f["ready_at"] == hm(13)),
    ("Valmis to. klo 10", MON10, lambda f: f["ready_at"] == 3 * D + hm(10)),
    ("Ready Monday 9:00", TUE10, lambda f: f["ready_at"] == 7 * D + hm(9)),
    ("Klar i övermorgon kl 9", MON10, lambda f: f["ready_at"] == 2 * D + hm(9)),
    ("Kiitos! 2 autoa tulossa, 30 km matka.", MON10, lambda f: not f["ready_at"] and not f["pickup_close"] and not f["deadline_at"]),
    ("Nouto klo 8-16", MON10, lambda f: f["pickup_open"] == hm(8) and f["pickup_close"] == hm(16)),
]

CASES_2 = [
    ("valmis noudettavaksi huomenna klo 7 jälkeen. noudettavissa ma-pe 9-15:30. kiitos", dhm(1, 10, 9), lambda f: f["ready_at"] == dhm(2, 7) and f["pickup_close"] == hm(15, 30)),
    ("Liike kiinni klo 16.30, haettava ennen sitä!", dhm(1, 10), lambda f: f["pickup_close"] == hm(16, 30) and f["delivery_close"] is None),
    ("plates removed, bring trade plates", 0, lambda f: f["plates"]),
    ("Huolto kesken, valmis to klo 13. Vastaanotto arkisin 7-20. Kiitos taas", dhm(1, 12, 35), lambda f: f["ready_at"] == dhm(3, 13) and f["delivery_close"] == hm(20)),
    ("Öppet 8 - 15.", 0, lambda f: f["pickup_close"] == hm(15)),
    ("huom aukioloajat klo 9.00 - 15.00", dhm(1, 12), lambda f: f["pickup_close"] == hm(15) and f["ready_at"] is None),
    ("oltava perillä viimeistään ma klo 20", dhm(1, 15), lambda f: f["deadline_at"] == dhm(7, 20)),
    ("Asiakas tulee hakemaan, perillä viim. huomenna klo 15.", dhm(1, 17), lambda f: f["deadline_at"] == dhm(2, 15)),
    ("Luovutus vasta kun maksettu, aikaisintaan huomenna yhdeksältä", dhm(2, 15), lambda f: f["release_at"] == dhm(3, 9)),
    ("hei. voi noutaa huomenna klo 10 alkaen. noudettavissa arkisin 7-18. soita kun olet lähellä", dhm(2, 17), lambda f: f["ready_at"] == dhm(3, 10)),
    ("toimitus perille viim. klo 14.00! nouto ennen klo 18.00", dhm(3, 9), lambda f: f["deadline_at"] == dhm(3, 14) and f["pickup_close"] == hm(18)),
    ("Auto valmis, nouto heti kun sopii. Liike kiinni klo 15.30, haettava ennen sitä. Lisäksi 4 kesärengasta tavaratilassa. Kiitti.", dhm(4, 11), lambda f: f["pickup_close"] == hm(15, 30) and not f["summer"]),
    ("Toimitus perille viim. klo 18. Kiitos.", dhm(0, 11), lambda f: f["deadline_at"] == dhm(0, 18)),
    ("haettavva ennen klo 16", dhm(0, 11), lambda f: f["pickup_close"] == hm(16)),
    ("Must be at destination by tomorrow 17:30. Correction: 16:30. Our ref: 880840", dhm(0, 19, 44), lambda f: f["deadline_at"] == dhm(1, 16, 30)),
    ("Saa hakea vasta huomenna klo 9 jälkeen. Toimitus perille viim. huomenna klo 19.", dhm(0, 20), lambda f: f["ready_at"] == dhm(1, 9) and f["deadline_at"] == dhm(1, 19)),
    ("ostaja maksaa ensin, auton saa huomenna puoli kahdesta alkaen", dhm(1, 9), lambda f: f["release_at"] == dhm(2, 13, 30)),
    ("Ostaja maksaa ensin, auton saa homenna klo 15 alkaen", dhm(1, 14), lambda f: f["release_at"] == dhm(2, 15)),
    ("Perillä trostaina klo 14 mennessä", dhm(1, 14), lambda f: f["deadline_at"] == dhm(3, 14)),
    ("Car has no plaates, bring trade plates.", 0, lambda f: f["plates"]),
    ("Noudettavissa arkisin 9 - 15", dhm(2, 18), lambda f: f["pickup_close"] == hm(15) and f["ready_at"] is None),
    ("Odottaa osia, valmis huomenna puoli kolmelta. Liike sulkee 18.00.", dhm(3, 18), lambda f: f["ready_at"] == dhm(4, 14, 30) and f["pickup_close"] == hm(18)),
    ("Auki vain klo 14.30 asti. Vastaanottava liike auki arkipäivisin klo 7 - 20", dhm(3, 14), lambda f: f["pickup_close"] == hm(14, 30) and f["delivery_close"] == hm(20)),
    ("Kohde auki arkisin klo 7-20. Huom aukioloajat arkisin klo 8-17.", dhm(4, 15), lambda f: f["pickup_close"] == hm(17)),
    ("Auto vielä kesärenkailla.", 0, lambda f: f["summer"]),
]


def run(cases):
    ok = 0
    for text, arrival, check in cases:
        f = parse_note(text, arrival)
        if check(f):
            ok += 1
        else:
            print("FAIL", text, {k: v for k, v in f.items() if k != "trace"}, f["trace"])
    print(f"{ok}/{len(cases)}")
    return ok == len(cases)


if __name__ == "__main__":
    good = run(CASES_1) and run(CASES_2)
    sys.exit(0 if good else 1)
