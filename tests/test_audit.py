"""Weekly CFO audit (D-088): safety check, accounting e-mail (content, privacy, CSV), owner report, send-once state."""

import csv
import io
from datetime import date, datetime

import pytest

from src.common.timeutil import WARSAW
from src.reports import audit
from src.reports.audit import (WeeklyAudit, audit_ready, bridge_sentence, bridge_sums, build_accounting_mail,
                               build_reminder_mail, cash_section, check_csv, csv_value, documents_owner_section,
                               documents_section, finance_section, mln, months_ranges, oss_overdue, table_rows, tax_line, zl)

MONDAY = date(2026, 11, 23)


def _report_row(entity, code, adresat="ksiegowosc", otwarte=3, nowe=1, poprawione=0, kwota=1000.0, typ="blad",
                label=None, sort=10, najstarsze_dni=0, kwota_poprawiona=None):
    return {"entity": entity, "check_code": code, "label": label or f"Kontrola {code} (opis)", "adresat": adresat,
            "co_zrobic": f"Zrób coś z {code}.", "sort": sort, "otwarte": otwarte, "nowe": nowe,
            "poprawione_ostatnio": poprawione, "ponownie_otwarte": 0, "wyjasnione": 0, "kwota_otwarta": kwota,
            "kwota_poprawiona_ostatnio": kwota_poprawiona, "najstarsze_od": "2026-11-03", "najstarsze_dni": najstarsze_dni,
            "przebieg": 2, "kwota_typ": typ}


def _finding(entity, code, i, amount=100.0, days=0, adresat="ksiegowosc", details=None, nowe=True):
    return {"entity": entity, "check_code": code, "kontrola": f"Kontrola {code}", "adresat": adresat, "item_key": f"k{i}",
            "label": f"Pozycja {code} {i}", "amount": amount, "status": "otwarte", "first_seen": "2026-11-03",
            "dni_otwarte": days, "nowe": nowe, "ponownie_otwarte": None, "poprawione_ostatnio": None, "note": None,
            "details": details or {}}


REPORT = [
    _report_row("delta", "delta_fv_podwojne", otwarte=20, nowe=2, kwota=50000.0, najstarsze_dni=17, sort=10),
    _report_row("delta", "delta_wyslane_bez_fv", adresat="wlasciciel", otwarte=2, kwota=260324.15, typ="informacja", sort=50),
    _report_row("delta", "delta_wyciagi_brak", otwarte=0, kwota=None, typ="informacja", sort=40),
    _report_row("agt", "agt_brak_listy_plac", otwarte=2, kwota=874380.55, typ="informacja", sort=40),
    _report_row("agt", "agt_wyciagi_brak", otwarte=1, kwota=None, typ="informacja", sort=50),
    _report_row("agt", "agt_fv_koszt_duble", otwarte=0, poprawione=4, kwota=0, kwota_poprawiona=1234.5, sort=30),
]
OPEN = (
    [_finding("delta", "delta_fv_podwojne", i, amount=1000.0 + i, days=17 if i % 2 else 3,
              details={"numery": "eu/1/09/2026, eu/2/09/2026", "fv_do_korekty": "eu/2/09/2026", "konto": "eur"})
     for i in range(20)]
    + [_finding("delta", "delta_wyslane_bez_fv", 1, amount=259573.65, adresat="wlasciciel")]
    + [_finding("agt", "agt_brak_listy_plac", i, amount=131970.68,
                details={"miesiac": f"2026-0{i + 8}-01", "pit_wyciag": 13812, "zus_wyciag": 40037.78, "netto_wyciag": 78120.9})
       for i in range(2)]
    + [_finding("agt", "agt_wyciagi_brak", 1, amount=None, details={"rachunek": "PKO-1", "nrb": "42102045640000500203961984"})]
)


# ─── Safety check ────────────────────────────────────────────────────────────


def test_audit_ready_only_with_todays_run_and_counts():
    run = {"id": 2, "run_at": "2026-11-23T04:30:05Z", "trigger": "cron", "otwarte": 10}
    assert audit_ready([run], MONDAY, REPORT) == (True, "")
    ok, why = audit_ready([{**run, "run_at": "2026-11-16T04:30:05Z"}], MONDAY, REPORT)
    assert not ok and "16.11.2026 05:30" in why and "nie z dzisiaj" in why  # time of the last run, Warsaw time
    assert not audit_ready([{**run, "otwarte": None}], MONDAY, REPORT)[0]
    assert not audit_ready([], MONDAY, REPORT)[0]
    assert not audit_ready([run], MONDAY, [])[0]
    # 23:30 UTC on Sunday is already Monday in Warsaw
    assert audit_ready([{**run, "run_at": "2026-11-22T23:30:00Z"}], MONDAY, REPORT)[0]


# ─── Mail 1: accounting office ───────────────────────────────────────────────


def test_accounting_mail_lists_only_open_accounting_checks_per_company():
    m = build_accounting_mail(REPORT, OPEN, MONDAY)
    body = m["markdown"]
    assert m["subject"] == "MyBed — kontrole dokumentów, tydzień 48 (23.11.2026)"
    assert body.index("## Delta Industries") < body.index("## All Good Things")
    assert "delta\\_wyslane\\_bez\\_fv" not in body and "Kontrola delta_wyslane_bez_fv" not in body  # owner's check
    assert "Kontrola delta_wyciagi_brak" not in body  # nothing open
    assert "**Co zrobić:** Zrób coś z delta\\_fv\\_podwojne." in body
    assert "**Otwarte:** 20 (w tym nowe: 2) · **Kwota do korekty:** 50" in body
    assert m["open"] == 23 and m["checks"] == 3
    assert "Delta: 20" in m["lead"] and "AGT: 3" in m["lead"]


def test_accounting_mail_urgent_items_top_15_and_csv_for_the_rest():
    m = build_accounting_mail(REPORT, OPEN, MONDAY)
    body = m["markdown"]
    delta = body.split("## All Good Things")[0]
    assert sum(1 for line in delta.splitlines() if line.startswith("- Pozycja delta")) == 15
    assert "pilne — otwarte od 17 dni" in delta and "otwarte od 3 dni" not in delta
    assert "**Pilne:** 10 pozycji otwartych dłużej niż 14 dni" in delta
    assert "Pozostałe 5 poz. w załączniku delta\\_fv\\_podwojne\\_2026-11-23.csv." in delta
    # largest first
    assert delta.index("Pozycja delta\\_fv\\_podwojne 19") < delta.index("Pozycja delta\\_fv\\_podwojne 18")
    assert {a["filename"] for a in m["attachments"]} == {
        "delta_fv_podwojne_2026-11-23.csv", "agt_brak_listy_plac_2026-11-23.csv", "agt_wyciagi_brak_2026-11-23.csv"}
    assert m["urgent"] == 10


def test_accounting_mail_information_amount_is_not_an_error_and_payroll_has_no_amounts():
    m = build_accounting_mail(REPORT, OPEN, MONDAY)
    agt = m["markdown"].split("## All Good Things")[1].split("## Dziękujemy")[0]
    assert "Brak listy płac za 08.2026" in agt and "Brak listy płac za 09.2026" in agt
    assert "zł" not in agt  # no payroll amounts (per person or total), statements have no amount
    assert "błąd" not in agt.lower() and "do korekty" not in agt
    payroll = next(a for a in m["attachments"] if "plac" in a["filename"]).get("content").decode("utf-8-sig")
    assert "131970" not in payroll and "13812" not in payroll and "pit" not in payroll.lower()
    assert payroll.splitlines()[0] == "Pozycja;Od kiedy na liście;Dni otwarte;Pilne (> 14 dni);Nowa;Miesiąc"
    assert "Brak listy płac za 08.2026;2026-11-03;0;;tak;2026-08" in payroll


def test_accounting_mail_thanks_for_fixed_items_without_financial_results():
    m = build_accounting_mail(REPORT, OPEN, MONDAY)
    body = m["markdown"]
    assert "## Dziękujemy — poprawione od zeszłego tygodnia" in body
    assert "All Good Things · Kontrola agt\\_fv\\_koszt\\_duble: 4 poz., 1 234,50 zł" in body
    for word in ("wynik", "przychód", "marża", "prognoza"):
        assert word not in body.lower()


def test_accounting_mail_info_check_amount_label():
    rows = [_report_row("delta", "delta_x", kwota=5000.0, typ="informacja")]
    body = build_accounting_mail(rows, [_finding("delta", "delta_x", 1)], MONDAY)["markdown"]
    assert "**Kwota:** 5 000,00 zł" in body and "korekty" not in body


def test_check_csv_format_bom_semicolon_decimal_comma_and_excel_safe_numbers():
    check = {"check_code": "delta_fv_koszt_duble"}
    items = [_finding("delta", "delta_fv_koszt_duble", 1, amount=212330.0, days=20,
                      details={"nip": "6192052377", "numer": "5/04/2026",
                               "wpisy": [{"id": 1, "data": "2026-02-28", "konto": "eur", "netto": 212330}]}),
             _finding("delta", "delta_fv_koszt_duble", 2, amount=53.65, details={"numer": "FA/191/03/2026"})]
    att = check_csv(check, items, MONDAY)
    raw = att["content"]
    assert raw.startswith("﻿".encode("utf-8")) and att["filename"] == "delta_fv_koszt_duble_2026-11-23.csv"
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:6] == ["Pozycja", "Kwota PLN (netto)", "Od kiedy na liście", "Dni otwarte", "Pilne (> 14 dni)", "Nowa"]
    assert rows[0][6:] == ["Numer FV", "NIP", "Wpisy w Fakturowni"]
    assert rows[1][1] == "212330,00" and rows[1][4] == "tak" and rows[1][6] == '="5/04/2026"'
    assert rows[1][8] == "id 1 · data 2026-02-28 · konto eur · netto 212330,00"
    assert rows[2][1] == "53,65" and rows[2][6] == "FA/191/03/2026"


def test_csv_value_groups_bank_account_and_shortens_months():
    assert csv_value("nrb", "42102045640000500203961984") == "42 1020 4564 0000 5002 0396 1984"
    assert csv_value("miesiac", "2026-09-01") == "2026-09"
    assert csv_value("x", 0.5) == "0,50" and csv_value("x", 7) == "7" and csv_value("x", True) == "tak"


# ─── Mail 2: owner's report ──────────────────────────────────────────────────


def test_finance_section_marks_payroll_estimates_and_survives_missing_parts():
    agt = [
        {"month": "2026-09-01", "miesiac_biezacy": False, "sprzedaz": 1065175, "wynik_przed_placami": 375782,
         "place": 131971, "place_zrodlo": "wyciag", "wynik_po_placach": 243811, "bank_wplywy": 100, "bank_wyplywy": 300,
         "bank_do": "2026-09-30"},
        {"month": "2026-10-01", "miesiac_biezacy": False, "sprzedaz": 1608045, "wynik_przed_placami": 711608,
         "place": 139164, "place_zrodlo": "lista", "wynik_po_placach": 572444},
        {"month": "2026-11-01", "miesiac_biezacy": True, "sprzedaz": 532933, "wynik_przed_placami": 112718},
    ]
    md, facts = finance_section({"agt": agt, "errors": {"overview": "CFO Studio: HTTP 500"}}, MONDAY)
    assert "~131 971 zł" in md and "| 139 164 zł |" in md
    assert "płace szacowane z wyciągów" in md
    assert "listopad 2026 (w toku)" in md
    assert "Niedostępne: CFO Studio: HTTP 500." in md  # Delta
    assert "rozliczenie w grupie" in md and "Gotówka AGT" not in md  # cash is commented for the group (D-089)
    assert "Brak zamkniętych miesięcy 2026" in md  # group
    assert facts["agt"][0]["place_zrodlo"] == "wyciag"


def test_money_formatting():
    assert zl(1234567.891) == "1 234 568 zł"
    assert zl(-402857, True) == "−402 857,00 zł"
    assert zl(None) == "—" and zl(-0.2) == "0 zł"
    assert table_rows({"columns": ["a", "b"], "rows": [[1, 2]]}) == [{"a": 1, "b": 2}]


# ─── Cash (D-089): result → change in cash, taxes to pay ─────────────────────


def _bridge(entity, month, wynik, zmiana, podatek, vat, wlasciciel, powiazane, podwojne, klienci, koszty):
    return {"entity": entity, "month": month, "wynik": wynik, "zmiana_gotowki": zmiana, "l_podatek": podatek, "l_vat": vat,
            "l_wlasciciel": wlasciciel, "l_powiazane": powiazane, "l_podwojne": podwojne, "l_klienci": klienci,
            "l_koszty": koszty}


# Grupa I–IX 2026 squeezed into two months with the same sums as CFO Studio's cash_bridge_monthly
BRIDGE = [
    _bridge("grupa", "2026-01-01", 3_000_000, 200_000, -986_619.56, -300_000, -300_000, -211_165, -300_000, 1_000_000,
            -1_702_215.44),
    _bridge("grupa", "2026-09-01", 2_024_193, 268_730.79, -1_000_000, -308_323.76, -276_121.05, -150_003, -388_987.01,
            1_439_832.43, -1_072_273.89),
    _bridge("delta", "2026-09-01", 925_762.9, 880_353.55, -4, 267_945.32, -45_000, 335_871.9, -420_687.98, 271_845.61,
            -455_380.2),
    _bridge("agt", "2026-10-01", 572_444, -396_444.17, 0, -114_494.43, 0, -311_271.9, -400_460.79, -106_648.72, -36_012.33),
    _bridge("grupa", "2025-12-01", 1, 1, 0, 0, 0, 0, 0, 0, 0),
]
RESERVE = [
    {"entity": "agt", "rok": 2026, "do_miesiaca": "2026-10-01", "oss_do_zaplaty": 0, "vat_kraj_do_zaplaty": 25526,
     "dochodowy_do_zaplaty": None},
    {"entity": "delta", "rok": 2026, "do_miesiaca": "2026-09-01", "oss_do_zaplaty": 942149, "vat_kraj_do_zaplaty": -588285,
     "dochodowy_do_zaplaty": 773231},
]


def test_bridge_sentence_follows_the_pattern():
    b = bridge_sums(BRIDGE, "grupa", 2026)
    assert b["months"] == ["2026-01-01", "2026-09-01"]  # 2025 left out
    assert bridge_sentence("Grupa", b) == (
        "Grupa — wynik 5,02 mln → na rachunkach +0,47 mln. Najwięcej zabrały: koszty i reszta −2,77 mln, "
        "podatek dochodowy −1,99 mln, podwójne FV −0,69 mln; dołożyli klienci +2,44 mln.")
    assert mln(-588285) == "−0,59 mln" and mln(2000, True) == "0,00 mln" and mln(None) == "—"


def test_cash_section_group_and_delta_taxes_and_urgent():
    md, facts = cash_section({"cash_bridge": BRIDGE, "tax_reserve": RESERVE}, date(2026, 11, 23), "https://cfo.example/grupa")
    assert "**Gotówka 2026 (I–IX):** Grupa — wynik 5,02 mln → na rachunkach +0,47 mln." in md
    assert "Delta (IX 2026) — wynik 0,93 mln → na rachunkach +0,88 mln." in md
    assert ("**Do zapłaty (szacunek):** Delta (I–IX 2026) — OSS 0,94 mln, PIT + danina 0,77 mln; nadwyżka VAT krajowego "
            "0,59 mln do odzyskania. AGT (I–X 2026) — VAT krajowy 0,03 mln.") in md
    assert "OSS po terminie — Delta 0,94 mln (szacunek; termin za III kw. 2026 minął 31.10)" in md
    assert "Rezerwa na PIT — Delta: szacunkowo 0,77 mln" in md
    assert "| **Zmiana gotówki** | **+0,47** | **+0,88** |" in md
    assert "[Od wyniku Grupy do gotówki](https://cfo.example/grupa)" in md
    assert facts["most"]["grupa"]["linie"]["Koszty i reszta"] == -2774489 and len(facts["pilne"]) == 2


def test_cash_section_without_bridge_or_reserve_does_not_guess():
    md, facts = cash_section({"cash_bridge": BRIDGE[-1:], "tax_reserve": []}, date(2026, 11, 23), "u")
    assert "Most gotówkowy niedostępny" in md and "mln" not in md
    assert "**Do zapłaty:** most gotówkowy niedostępny" in md  # taxes too
    md, _ = cash_section({"cash_bridge": BRIDGE, "tax_reserve": [], "errors": {"tax_reserve": "HTTP 500"}},
                         date(2026, 11, 23), "u")
    assert "Grupa — wynik 5,02 mln" in md and "**Do zapłaty:** most gotówkowy niedostępny (HTTP 500)." in md


def test_oss_deadline_and_tax_line_edges():
    row = {"entity": "delta", "rok": 2026, "do_miesiaca": "2026-09-01", "oss_do_zaplaty": 1000}
    assert oss_overdue(row, date(2026, 10, 31)) is None  # deadline day itself
    assert "31.10" in oss_overdue(row, date(2026, 11, 1))
    assert oss_overdue({**row, "do_miesiaca": "2026-02-01"}, date(2026, 11, 1)) is None  # no full quarter yet
    assert "30.04" in oss_overdue({**row, "do_miesiaca": "2026-04-01"}, date(2026, 5, 4))
    assert "31.01" in oss_overdue({**row, "do_miesiaca": "2026-12-01"}, date(2027, 2, 1))
    assert oss_overdue({**row, "oss_do_zaplaty": 0}, date(2026, 11, 1)) is None
    assert tax_line({"entity": "agt", "rok": 2026, "do_miesiaca": "2026-10-01", "oss_do_zaplaty": 0,
                     "vat_kraj_do_zaplaty": 0, "dochodowy_do_zaplaty": None}) == "AGT (I–X 2026) — nic do zapłaty"


# ─── Document requests (D-096) ───────────────────────────────────────────────


def _doc(code, entity, label, month, days, remind=True, reminders=0, url="https://drive.google.com/drive/folders/x1"):
    return {"source_code": code, "entity": entity, "rodzaj": "wyciag_rachunek", "label": label, "folder_url": url,
            "month": month, "termin": "2026-11-07", "dni_po_terminie": days, "status": "otwarte" if remind else "dostarczone",
            "brak_teraz": True, "do_przypomnienia": remind, "reminders": reminders, "fulfilled_at": None, "sort": 10}


DOCS = (
    [_doc("wyciag:PKO-1", "delta", "PKO BP rachunek VAT (Delta)", f"2026-{m:02d}-01", 300 - 30 * m, reminders=12)
     for m in range(1, 11)]
    + [_doc("wyciag:ING-5276", "delta", "ING Konto PLN …5276", "2026-10-01", 13, reminders=3)]
    + [_doc("lista_plac:agt", "agt", "Lista płac AGT (suma listy, PDF)", m, d, reminders=2,
            url="https://drive.google.com/drive/folders/x2") for m, d in (("2025-09-01", 404), ("2025-10-01", 373))]
    + [_doc("wyciag:ING-GBP", "delta", "ING Konto (GBP)", "2026-10-01", 13, remind=False)]  # uploaded, waits for import
)


def test_months_ranges_compress_consecutive_months():
    assert months_ranges(["2025-11-01", "2025-12-01", "2026-01-01", "2026-03-01", "2026-04-01", "2026-06-01"]) == \
        "11.2025–01.2026, 03.2026, 04.2026, 06.2026"
    assert months_ranges(["2026-10-01"]) == "10.2026" and months_ranges([]) == ""


def test_documents_section_per_company_with_folder_links_and_urgent_marks():
    md = documents_section(DOCS)
    assert md.startswith("## Prosimy o dokumenty")
    assert md.index("### Delta Industries") < md.index("### All Good Things")
    assert ("- **PKO BP rachunek VAT (Delta)** · brak: 01.2026–10.2026 · 270 dni po terminie · **pilne** · "
            "[wrzućcie plik tutaj](https://drive.google.com/drive/folders/x1)") in md
    assert "- **ING Konto PLN …5276** · brak: 10.2026 · 13 dni po terminie · [wrzućcie plik tutaj]" in md  # not urgent
    assert "brak: 09.2025, 10.2025 · 404 dni po terminie · **pilne**" in md
    assert "ING Konto (GBP)" not in md  # do_przypomnienia = false is never reminded
    assert "W CFO Studio brakuje 13 poz. (pilne — ponad 14 dni po terminie: 11)" in md
    assert "MT940" in md and "paski nie są potrzebne" in md
    assert documents_section([d for d in DOCS if not d["do_przypomnienia"]]) == ""


def test_accounting_mail_puts_document_requests_on_top_and_goes_out_for_documents_alone():
    m = build_accounting_mail(REPORT, OPEN, MONDAY, documents=DOCS)
    assert m["markdown"].startswith("## Prosimy o dokumenty") and m["documents"] == 13
    assert m["lead"].startswith("Dzień dobry, na górze prosimy o brakujące dokumenty (13 poz., w tym pilnych: 11)")
    assert len(m["document_rows"]) == 13
    only_docs = build_accounting_mail([], [], MONDAY, documents=DOCS)
    assert only_docs["checks"] == 0 and only_docs["documents"] == 13


def test_reminder_mail_lists_urgent_first_and_counts_positions():
    m = build_reminder_mail(DOCS, MONDAY)
    assert m["subject"] == "MyBed — przypomnienie: brakujące dokumenty (13 poz.)"
    body = m["markdown"]
    assert body.index("## Pilne — ponad 14 dni po terminie") < body.index("## Pozostałe")
    assert body.index("Lista płac AGT") < body.index("PKO BP rachunek VAT (Delta)") < body.index("ING Konto PLN")
    assert "**Delta · ING Konto PLN …5276**" in body and "**AGT · Lista" not in body  # entity added only when missing
    assert "Przypominamy codziennie, dopóki dokument nie trafi do folderu — po wgraniu przypomnienia ustają same." in body
    assert "ING Konto (GBP)" not in body and m["urgent"] == 11


def test_owner_documents_section_counts_oldest_reminders_delivered_and_escalation():
    fin = {"documents": DOCS, "errors": {},
           "document_reminders": [{"sent_at": "2026-11-23T06:00:01Z", "kind": "tygodniowy"},
                                  {"sent_at": "2026-11-20T06:00:01Z", "kind": "codzienny"},
                                  {"sent_at": "2026-11-10T06:00:01Z", "kind": "codzienny"}],
           "documents_delivered": [{"entity": "delta", "label": "ING Konto (USD)", "month": "2026-10-01",
                                    "status": "zrealizowane", "fulfilled_at": "2026-11-19T10:00:00Z"},
                                   {"entity": "delta", "label": "Karta …4141", "month": "2026-10-01",
                                    "status": "wstrzymane", "fulfilled_at": "2026-11-19T10:00:00Z"}]}
    md, facts = documents_owner_section(fin, MONDAY)
    assert "Brakuje **13** dokumentów (Delta 11, AGT 2); najstarszy: Lista płac AGT (suma listy, PDF) za 09.2025 — 404 dni po terminie." in md
    assert "Przypomnień w ostatnich 7 dniach: **2** (tygodniowe 1, codzienne 1)." in md
    assert "Dostarczone w ostatnich 7 dniach: ING Konto (USD) 10.2026." in md and "Karta" not in md
    # > 30 days AND >= 10 reminders: only the Delta PKO VAT months up to VIII (IX = 30 days, X = 0) — not the payroll (2 reminders)
    assert "**Do eskalacji**" in md and "- PKO BP rachunek VAT (Delta): 01.2026–08.2026 · 270 dni po terminie · 12 przypomnień" in md
    assert "Lista płac AGT (suma listy, PDF): " not in md.split("**Do eskalacji**")[1]
    assert facts["do_eskalacji"][0]["przypomnienia"] == 12
    md, _ = documents_owner_section({"errors": {"documents": "HTTP 500"}}, MONDAY)
    assert "Prośby o dokumenty niedostępne (HTTP 500)." in md


# ─── Orchestration: send once, safety, test mode ─────────────────────────────


class FakeClient:
    url = "https://cfo.example"

    def __init__(self, run_at="2026-11-23T04:30:05Z", fail=False, documents=None, record_fails=False):
        self.run_at, self.fail = run_at, fail
        self.documents = DOCS if documents is None else documents
        self.record_fails = record_fails
        self.calls = []

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if self.record_fails:
            raise audit.CFOStudioError("CFO Studio: HTTP 500")
        return '{"ok": true}'

    async def call_json(self, tool, args):
        if self.fail:
            raise audit.CFOStudioError("CFO Studio nie odpowiada: ConnectError")
        name = args["dataset"]
        if name == "audit_runs":
            rows = [{"id": 7, "run_at": self.run_at, "trigger": "cron", "otwarte": 25, "nowe": 3, "poprawione": 4,
                     "ponownie": 0, "kwota_otwarta": 1e6, "kwota_bledow": 50000.0},
                    {"id": 6, "run_at": "2026-11-16T04:30:05Z", "trigger": "cron", "otwarte": 27, "nowe": 0,
                     "poprawione": 0, "ponownie": 0, "kwota_otwarta": 1.1e6, "kwota_bledow": 61000.0}]
        elif name == "audit_report":
            rows = REPORT
        elif name == "audit_findings":
            rows = OPEN if args["filters"][0]["column"] == "status" else []
        elif name == "cash_bridge_monthly":
            rows = BRIDGE
        elif name == "tax_reserve":
            rows = RESERVE
        elif name == "document_requests":
            col = args["filters"][0]["column"]
            rows = [d for d in self.documents if d["do_przypomnienia"]] if col == "do_przypomnienia" else []
        else:
            rows = []
        cols = list(rows[0]) if rows else []
        return {"columns": cols, "rows": [[r.get(c) for c in cols] for r in rows], "truncated": False}

    async def overview(self):
        raise audit.CFOStudioError("brak")


class FakeDB:
    def __init__(self):
        self.settings = {"general": {"recipients": ["owner@mybed.pl"]}}
        self.saved = []

    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value

    async def get_reports(self, **kw):
        return []

    async def save_report(self, **kw):
        self.saved.append(kw)
        return len(self.saved)


class FakeAI:
    async def write(self, **kw):
        return "Komentarz agenta."


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    async def fake_send(to, subject, html, text, **kw):
        sent.append({"to": to, "subject": subject, "html": html, **kw})
        return "resend"

    monkeypatch.setattr(audit, "send_email", fake_send)
    monkeypatch.setattr(audit, "email_configured", lambda: "resend")
    monkeypatch.setattr(audit, "now_local", lambda: datetime(2026, 11, 23, 7, 0, tzinfo=WARSAW))
    return sent


async def test_run_sends_accounting_mail_with_cc_and_csv_then_owner_report_once(outbox):
    db = FakeDB()
    res = await WeeklyAudit(db, client=FakeClient(), ai=FakeAI()).run(scheduled=True)
    assert "error" not in res, res
    acc, own = outbox
    assert acc["to"] == ["ksiegowosc@mybed.pl"] and acc["cc"] == ["owner@mybed.pl"] and acc["reply_to"] == ["owner@mybed.pl"]
    assert acc["subject"].startswith("MyBed — kontrole dokumentów, tydzień 48")
    assert len(acc["attachments"]) == 3 and all(a["content_type"].startswith("text/csv") for a in acc["attachments"])
    assert own["to"] == ["owner@mybed.pl"] and own["subject"] == "Raport CFO — tydzień 48 · Delta + AGT"
    assert "Komentarz agenta." in own["html"] and "https://cfo.example/audyt" in own["html"]
    assert "−11 000 zł vs poprzedni przebieg" in own["html"]
    # cash and taxes (D-089) only in the owner's report, never in the accounting e-mail
    assert "Gotówka 2026 (I–IX)" in own["html"] and "OSS po terminie" in own["html"]
    for word in ("Gotówka", "gotówk", "OSS", "PIT", "danina", "wynik"):
        assert word not in acc["html"]
    assert [s["agent_name"] for s in db.saved] == ["audit_ksiegowosc", "weekly_audit"]
    assert "Prosimy o dokumenty" in acc["html"] and "wrzućcie plik tutaj" in acc["html"]
    assert db.settings["weekly_audit_state"]["accounting"]["run_id"] == 7
    # a second run (restart, retry, click) sends nothing
    again = await WeeklyAudit(db, client=FakeClient(), ai=FakeAI()).run(scheduled=True)
    assert len(outbox) == 2 and "wysłany już" in again["accounting"]["skipped"] and "owner" in again


async def test_run_without_todays_audit_only_tells_the_owner_once(outbox):
    db = FakeDB()
    res = await WeeklyAudit(db, client=FakeClient(run_at="2026-11-16T04:30:05Z"), ai=FakeAI()).run(scheduled=True)
    assert "Audyt się nie wykonał" in res["error"]
    assert len(outbox) == 1 and outbox[0]["to"] == ["owner@mybed.pl"] and "ksiegowosc@mybed.pl" not in str(outbox[0])
    assert "16.11.2026 05:30" in outbox[0]["html"]
    await WeeklyAudit(db, client=FakeClient(run_at="2026-11-16T04:30:05Z"), ai=FakeAI()).run(scheduled=True)
    assert len(outbox) == 1


async def test_run_when_cfo_studio_is_down_sends_nothing_to_accounting(outbox):
    res = await WeeklyAudit(FakeDB(), client=FakeClient(fail=True), ai=FakeAI()).run(scheduled=True)
    assert "nie odpowiada" in res["error"] and len(outbox) == 1 and outbox[0]["to"] == ["owner@mybed.pl"]


async def test_test_mode_sends_both_only_to_the_test_address_and_keeps_state(outbox):
    db = FakeDB()
    await WeeklyAudit(db, client=FakeClient(), ai=FakeAI()).run(test_to=["me@mybed.pl"])
    assert [m["to"] for m in outbox] == [["me@mybed.pl"], ["me@mybed.pl"]]
    assert all(m["subject"].startswith("[TEST] ") for m in outbox) and not outbox[0]["cc"]
    assert "weekly_audit_state" not in db.settings


async def test_disabled_in_studio_skips_scheduled_run(outbox):
    db = FakeDB()
    db.settings["weekly_audit"] = {"enabled": False}
    assert "skipped" in await WeeklyAudit(db, client=FakeClient(), ai=FakeAI()).run(scheduled=True)
    assert outbox == []


async def test_comment_failure_still_sends_the_report(outbox):
    class BrokenAI:
        async def write(self, **kw):
            raise RuntimeError("Na koncie Anthropic API skończyły się środki")

    db = FakeDB()
    await WeeklyAudit(db, client=FakeClient(), ai=BrokenAI()).run()
    assert len(outbox) == 2 and "Komentarz niedostępny" in outbox[1]["html"]
    assert "komentarz" in db.saved[-1]["meta"]["errors"]


# ─── E-mail output, scheduler ────────────────────────────────────────────────


def test_email_message_carries_cc_reply_to_and_csv_attachment():
    from src.outputs.email_output import build_message

    msg = build_message(["a@x.pl"], "S", "<p>h</p>", "t", cc=["b@x.pl"], reply_to=["c@x.pl"],
                        attachments=[{"filename": "x.csv", "content": "﻿a;b\r\n".encode(), "content_type": "text/csv; charset=utf-8"}])
    assert msg["Cc"] == "b@x.pl" and msg["Reply-To"] == "c@x.pl"
    att = next(msg.iter_attachments())
    assert att.get_filename() == "x.csv" and att.get_content_type() == "text/csv"
    assert att.get_payload(decode=True).startswith("﻿".encode())


async def test_weekly_mail_records_the_document_reminder_once_and_never_in_test_mode(outbox):
    client, db = FakeClient(), FakeDB()
    await WeeklyAudit(db, client=client, ai=FakeAI()).run(scheduled=True)
    (tool, args), = client.calls
    assert tool == "document_reminder_sent" and args["kind"] == "tygodniowy"
    assert args["recipients"] == "ksiegowosc@mybed.pl; kopia: owner@mybed.pl"
    assert len(args["items"]) == 13 and {"source_code": "wyciag:ING-5276", "month": "2026-10-01"} in args["items"]
    await WeeklyAudit(db, client=client, ai=FakeAI()).run(scheduled=True)  # already sent → no second record
    assert len(client.calls) == 1
    test_client = FakeClient()
    await WeeklyAudit(FakeDB(), client=test_client, ai=FakeAI()).run(test_to=["me@mybed.pl"])
    await WeeklyAudit(FakeDB(), client=test_client, ai=FakeAI()).run(dry_run=True, with_ai=False)
    assert test_client.calls == []


async def test_weekly_mail_record_failure_is_reported_but_mail_counts_as_sent(outbox):
    db = FakeDB()
    res = await WeeklyAudit(db, client=FakeClient(record_fails=True), ai=FakeAI()).run(scheduled=True)
    assert "document_reminder_sent" in res["error"] and db.settings["weekly_audit_state"]["accounting"]["run_id"] == 7
    assert "Nie udało się zapisać przypomnienia w CFO Studio: CFO Studio: HTTP 500." in outbox[1]["html"]


async def test_daily_reminder_sends_once_a_day_and_records_it(outbox):
    client, db = FakeClient(), FakeDB()
    res = await WeeklyAudit(db, client=client).remind(scheduled=True)
    assert res["count"] == 13 and res["recorded"]
    mail, = outbox
    assert mail["to"] == ["ksiegowosc@mybed.pl"] and mail["cc"] == ["owner@mybed.pl"] and mail["reply_to"] == ["owner@mybed.pl"]
    assert mail["subject"] == "MyBed — przypomnienie: brakujące dokumenty (13 poz.)"
    assert "wrzućcie plik tutaj" in mail["html"] and "ING Konto (GBP)" not in mail["html"]
    (tool, args), = client.calls
    assert tool == "document_reminder_sent" and args["kind"] == "codzienny" and len(args["items"]) == 13
    assert db.saved[-1]["agent_name"] == "audit_dokumenty"
    again = await WeeklyAudit(db, client=client).remind(scheduled=True)
    assert "już dziś" in again["skipped"] and len(outbox) == 1 and len(client.calls) == 1


async def test_daily_reminder_sends_nothing_when_nothing_is_missing_or_switched_off(outbox):
    client = FakeClient(documents=[d for d in DOCS if not d["do_przypomnienia"]])
    res = await WeeklyAudit(FakeDB(), client=client).remind(scheduled=True)
    assert "skipped" in res and outbox == [] and client.calls == []
    db = FakeDB()
    db.settings["weekly_audit"] = {"doc_reminders": False}
    assert "wyłączone" in (await WeeklyAudit(db, client=FakeClient()).remind(scheduled=True))["skipped"]
    assert outbox == []


async def test_daily_reminder_test_mode_goes_only_to_the_owner_without_recording(outbox):
    client, db = FakeClient(), FakeDB()
    await WeeklyAudit(db, client=client).remind(test_to=["me@mybed.pl"])
    assert outbox[0]["to"] == ["me@mybed.pl"] and not outbox[0]["cc"] and outbox[0]["subject"].startswith("[TEST] ")
    assert client.calls == [] and "doc_reminder_state" not in db.settings


def test_weekly_audit_is_scheduled_monday_7_warsaw():
    from src import jobs
    from src.scheduler import DEFAULT_CRONS, MISFIRE_GRACE, TZ

    assert DEFAULT_CRONS["weekly_audit"] == "0 7 * * mon" and TZ == "Europe/Warsaw"
    assert "weekly_audit" in jobs.JOBS and MISFIRE_GRACE["weekly_audit"] >= 3600
    assert DEFAULT_CRONS["doc_reminder"] == "0 7 * * tue-fri" and "doc_reminder" in jobs.JOBS
    assert set(jobs.JOBS) <= set(DEFAULT_CRONS)
