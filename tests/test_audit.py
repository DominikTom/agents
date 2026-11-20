"""Weekly CFO audit (D-088): safety check, accounting e-mail (content, privacy, CSV), owner report, send-once state."""

import csv
import io
from datetime import date, datetime

import pytest

from src.common.timeutil import WARSAW
from src.reports import audit
from src.reports.audit import (WeeklyAudit, audit_ready, build_accounting_mail, check_csv, csv_value, finance_section,
                               table_rows, zl)

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
    assert "przepływ netto **−200 zł**" in md
    assert "Brak zamkniętych miesięcy 2026" in md  # group
    assert facts["agt"][0]["place_zrodlo"] == "wyciag"


def test_money_formatting():
    assert zl(1234567.891) == "1 234 568 zł"
    assert zl(-402857, True) == "−402 857,00 zł"
    assert zl(None) == "—" and zl(-0.2) == "0 zł"
    assert table_rows({"columns": ["a", "b"], "rows": [[1, 2]]}) == [{"a": 1, "b": 2}]


# ─── Orchestration: send once, safety, test mode ─────────────────────────────


class FakeClient:
    url = "https://cfo.example"

    def __init__(self, run_at="2026-11-23T04:30:05Z", fail=False):
        self.run_at, self.fail = run_at, fail

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
    assert [s["agent_name"] for s in db.saved] == ["audit_ksiegowosc", "weekly_audit"]
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


def test_weekly_audit_is_scheduled_monday_7_warsaw():
    from src import jobs
    from src.scheduler import DEFAULT_CRONS, MISFIRE_GRACE, TZ

    assert DEFAULT_CRONS["weekly_audit"] == "0 7 * * mon" and TZ == "Europe/Warsaw"
    assert "weekly_audit" in jobs.JOBS and MISFIRE_GRACE["weekly_audit"] >= 3600
    assert set(jobs.JOBS) <= set(DEFAULT_CRONS)
