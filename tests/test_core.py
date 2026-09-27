"""Unit tests for pure logic: rendering, auth, profiles, dash math, OS views, chat parsing."""

from datetime import date, timedelta

from src.common.timeutil import today
from src.connectors.dash import build_overview, normalize_spend
from src.connectors.os_mybed import OSSnapshot
from src.dashboard.auth import make_token, verify_login, verify_session
from src.ingestion.whatsapp_ingest import render_body
from src.processing.chat_digest import fingerprint
from src.reports.engine import build_prompt
from src.reports.profiles import DEFAULT_PROFILES, SECTION_CATALOG, merge_general, merge_profiles
from src.reports.render import email_html, split_lead, to_html


def test_markdown_render_escapes_html_and_bad_links():
    html = to_html("## A\n<script>x</script> [x](javascript:alert(1)) [ok](https://os.mybed.cloud)")
    assert "<script>" not in html
    assert "javascript:" not in html
    assert 'href="https://os.mybed.cloud"' in html


def test_split_lead_strips_markdown_and_separates_body():
    lead, body = split_lead("Dziś **dwie** sprawy.\n\n## Sekcja\n- a")
    assert lead == "Dziś dwie sprawy."
    assert body.startswith("## Sekcja")


def test_email_html_inlines_styles():
    out = email_html(label="Raport", title="T", body_html="<h2>X</h2><p>y</p>", url="https://x", footer="f")
    assert 'style="' in out and "Otwórz w panelu" in out


def test_session_tokens_roundtrip_and_tamper():
    token = make_token("admin")
    assert verify_session(token)
    assert not verify_session(token[:-2] + "xx")
    assert not verify_session(None)
    assert verify_login("admin", "wrong") is None
    assert verify_session(verify_login("admin", "test-password"))


def test_profiles_merge_keeps_user_choices_and_adds_new_sections():
    stored = {"morning_briefing": {"time": "06:30", "sections": [{"key": "sales", "enabled": True, "note": "x"}, {"key": "bogus"}]}}
    merged = merge_profiles(stored)
    mb = merged["morning_briefing"]
    assert mb["time"] == "06:30"
    assert mb["sections"][0] == {"key": "sales", "enabled": True, "note": "x"}
    keys = [s["key"] for s in mb["sections"]]
    assert "bogus" not in keys and set(keys) == set(SECTION_CATALOG)
    assert all(not s["enabled"] for s in mb["sections"][1:] if not SECTION_CATALOG[s["key"]].get("auto_enable"))
    assert set(merged) == set(DEFAULT_PROFILES)


def test_prompt_lists_enabled_sections_in_order_with_notes():
    p = merge_profiles({})["morning_briefing"]
    p["sections"][1]["note"] = "tylko zarząd"
    p["instructions"] = "Pisz krótko"
    prompt = build_prompt(p, merge_general({"vip": ["Maciej Żydziak"]}), "morning")
    first = SECTION_CATALOG[p["sections"][0]["key"]]["label"]
    assert f"1. ## {first}" in prompt
    assert "tylko zarząd" in prompt and "Pisz krótko" in prompt and "Maciej Żydziak" in prompt
    assert SECTION_CATALOG["slack"]["label"] not in prompt  # disabled by default


def test_dash_overview_math():
    ref = date(2026, 9, 22)
    rows = []
    for i in range(20):
        d = (ref - timedelta(days=i)).isoformat()
        rows.append({"date": d, "source_shop": "mybed.pl", "orders_count": 10, "revenue_gross_pln": 1000 + (100 if i == 0 else 0),
                     "revenue_paid_pln": 900, "avg_order_value_pln": 100, "revenue_gross_original": 1000, "original_currency": "PLN"})
    rows.append({"date": ref.isoformat(), "source_shop": "mybed.de", "orders_count": 1, "revenue_gross_pln": 430,
                 "revenue_paid_pln": 430, "avg_order_value_pln": 430, "revenue_gross_original": 100, "original_currency": "EUR"})
    meta = [{"date": ref.isoformat(), "platform": "meta", "account_id": "act_1681802382204753", "spend": 100},
            {"date": ref.isoformat(), "platform": "meta", "account_id": "act_637792865917248", "spend": 42,
             "spend_original": 10, "original_currency": "EUR"}]
    google = [{"date": ref.isoformat(), "hostname": "mybed.pl", "ad_cost": 300},
              {"date": ref.isoformat(), "hostname": "mybed.de", "ad_cost": 10}]  # EUR → 42 zł
    ads = normalize_spend(meta, google, rows)
    rooms = [{"showroom": "warszawa", "date": ref.isoformat(), "orders": 2, "revenue_pln": 5000},
             {"showroom": "krakow", "date": "2026-12-04", "orders": 1, "revenue_pln": 999}]  # future row ignored
    o = build_overview(rows, ads, rooms, ref)
    assert o["total"]["revenue"] == 1530
    m = o["marketing"]
    assert m["spend_day"] == 484.0
    pl, de, mitto = m["per_sklep"][:3]
    assert (pl["shop"], pl["Meta"]["dzien"], pl["Google Ads"]["dzien"], pl["suma_dzien"]) == ("mybed.pl", 100, 300, 400)
    assert (de["Meta"]["dzien"], de["Google Ads"]["dzien"]) == (42, 42)
    assert mitto["suma_dzien"] == 0
    assert {p["platforma"]: p["dzien"] for p in m["per_platforma"]} == {"Meta": 142, "Google Ads": 342}
    assert "mer_day" not in m
    assert [s["showroom"] for s in o["showrooms"]] == ["warszawa"]
    assert len(o["series"]) == 30


def test_os_snapshot_views():
    t = today()
    past, fut = (t - timedelta(days=3)).isoformat(), (t + timedelta(days=3)).isoformat()
    data = {
        "people": [{"id": "p-dominik", "name": "Dominik Tomaszczyk"}, {"id": "p-a", "name": "Anna Nowak"}],
        "projects": [{"id": "pr1", "title": "Launch DE", "status": "In Progress", "health": "red", "ownerId": "p-a"},
                     {"id": "pr2", "title": "Done one", "status": "Done", "health": "red"}],
        "tasks": [
            {"id": "t1", "title": "Mine overdue", "status": "Todo", "assigneeId": "p-dominik", "dueDate": past},
            {"id": "t2", "title": "Mine today", "status": "Doing", "assigneeId": "p-dominik", "dueDate": t.isoformat()},
            {"id": "t3", "title": "Anna overdue", "status": "Todo", "assigneeId": "p-a", "dueDate": past, "projectId": "pr1"},
            {"id": "t4", "title": "Done", "status": "Done", "assigneeId": "p-a", "dueDate": past},
            {"id": "t5", "title": "Future", "status": "Todo", "assigneeId": "p-dominik", "dueDate": fut},
        ],
        "blockers": [{"title": "B", "status": "Open", "ownerId": "p-a"}, {"title": "R", "status": "Resolved"}],
        "decisions": [{"title": "D", "status": "Proposed"}, {"title": "E", "status": "Decided"}],
    }
    s = OSSnapshot(data, "p-dominik", "https://os.mybed.cloud")
    mine = s.my_tasks()
    assert [x["title"] for x in mine["overdue"]] == ["Mine overdue"]
    assert [x["title"] for x in mine["today"]] == ["Mine today"]
    assert mine["overdue"][0]["link"] == "https://os.mybed.cloud/?p=task:t1"
    team = s.team_load()
    assert team[0]["person"] == "Anna Nowak" and team[0]["overdue"] == 1
    assert [b["title"] for b in s.blockers()] == ["B"]
    assert [p["title"] for p in s.projects_attention()] == ["Launch DE"]
    assert len(s.decisions_pending()) == 1


def test_whatsapp_render_body():
    assert render_body({"type": "audio", "durationSec": 42, "isVoiceNote": True}) == "[głosówka 0:42]"
    assert render_body({"type": "document", "fileName": "oferta.pdf", "body": "oferta.pdf"}) == "[dokument: oferta.pdf]"
    assert render_body({"type": "image", "body": "nowe kreacje"}) == "[zdjęcie] nowe kreacje"
    assert render_body({"type": "text", "body": "hej"}) == "hej"


def test_commitment_fingerprint_ignores_word_order_and_punctuation():
    a = fingerprint("whatsapp", "jid", "mine", "Wyślij Sandrze cennik B2B!")
    b = fingerprint("whatsapp", "jid", "mine", "cennik b2b wyślij sandrze")
    assert a == b
    assert a != fingerprint("whatsapp", "other", "mine", "Wyślij Sandrze cennik B2B")


def test_kpi_periods_and_table():
    from src.reports.kpi import headline_cards, kpi_data, kpi_email_html, kpi_markdown, render_kpi, tables

    ref = date(2026, 9, 22)
    rows = [{"date": (ref - timedelta(days=i)).isoformat(), "source_shop": "mybed.pl", "orders_count": 1,
             "revenue_gross_pln": 200 if i < 7 else 100, "revenue_paid_pln": 0, "avg_order_value_pln": 0,
             "revenue_gross_original": 0, "original_currency": "PLN"} for i in range(60)]
    ads = [{"date": (ref - timedelta(days=i)).isoformat(), "shop": "mybed.pl", "platform": "google", "spend": 10}
           for i in range(60)]
    o = build_overview(rows, ads, [], ref)
    week = o["periods"]["7"]
    assert (week["from"], week["prev_to"]) == ("2026-09-16", "2026-09-15")
    total = week["rows"][0]
    assert total["revenue"]["v"] == 1400 and total["revenue"]["pct"] == 100.0
    assert total["spend_google"]["v"] == 70 and total["spend_meta"]["v"] == 0

    cfg = {"enabled": True, "periods": ["1", "7"], "metrics": ["revenue", "orders", "spend_total"], "per_shop": True}
    data = kpi_data(o, cfg)
    assert [p["key"] for p in data["periods"]] == ["1", "7"] and data["as_of"] == "2026-09-22"
    cards = headline_cards(data)
    assert [c["label"] for c in cards] == ["Przychód", "Zamówienia", "Reklamy razem"]
    assert cards[0]["tone"] == "neutral"  # yesterday = day before → ±0
    week_cards = headline_cards(kpi_data(o, {**cfg, "periods": ["7"]}))
    assert week_cards[0]["tone"] == "good" and week_cards[2]["tone"] == "neutral"  # spend deltas are never red/green
    t7 = tables(data)[1]
    assert t7["columns"] == ["Razem", "mybed.pl", "mybed.de", "MittoHome"]  # rows = metrics, columns = shops
    assert t7["rows"][0]["cells"][0]["value"] == "1\u00a0400" and t7["rows"][0]["cells"][0]["pct"] == "+100,0%"
    assert t7["rows"][0]["unit"] == "zł" and t7["rows"][1]["unit"] == ""

    md = render_kpi(o, cfg)
    assert "## Liczby" in md and "| Przychód (zł) | 1 400 (+100,0%)" in md and "16.09–22.09 vs 09.09–15.09" in md
    assert kpi_markdown(kpi_data(o, {**cfg, "enabled": False})) == ""
    flat = kpi_markdown(kpi_data(o, {"enabled": True, "periods": ["30"], "metrics": ["orders"], "per_shop": False}))
    assert "| Zamówienia | 30 (±0,0%) |" in flat

    mail = kpi_email_html(data)
    assert "#047857" in mail and "1\u00a0400" in mail and "<script" not in mail


def test_email_template_numeric_cells_and_escaping():
    from src.reports.render import email_html, to_html

    body = to_html("| Sklep | Zam. |\n|---|---|\n| mybed.pl | **52** |\n\n<script>alert(1)</script>")
    mail = email_html(label="Raport", title="T <b>", body_html=body, url=None, footer="f", lead="Lead & co",
                      kpi_html="", sources="WhatsApp, Gmail")
    assert "text-align:right" in mail and "text-align:left" in mail
    assert mail.index("Zam.") > mail.index("text-align:right")  # numeric column header aligned with its numbers
    assert "<script>" not in mail and "T &lt;b&gt;" in mail and "Lead &amp; co" in mail
    assert "Źródła:" in mail


def test_new_auto_enabled_section_lands_next_to_its_neighbour():
    stored = {"morning_briefing": {"sections": [
        {"key": k, "enabled": True, "note": ""} for k in ["top", "awaiting", "calendar", "sales", "marketing", "chats", "commitments"]
    ]}}
    keys = [s["key"] for s in merge_profiles(stored)["morning_briefing"]["sections"]]
    enabled = {s["key"]: s["enabled"] for s in merge_profiles(stored)["morning_briefing"]["sections"]}
    assert keys.index("team_updates") == keys.index("chats") + 1 and enabled["team_updates"]
    assert not enabled["projects"]  # regular new sections stay off


def test_slack_chunks_keep_code_fences_balanced():
    from src.outputs.slack_output import _chunks, md_to_slack

    text = md_to_slack("## A\n\n| a | b |\n|---|---|\n" + "| x | 1 |\n" * 400)
    parts = _chunks(text, 1000)
    assert len(parts) > 1 and all(p.count("```") % 2 == 0 for p in parts)


def test_team_updates_last_workday():
    from src.processing.team_updates import last_workday

    assert last_workday(date(2026, 9, 28)) == date(2026, 9, 25)  # Monday → Friday
    assert last_workday(date(2026, 9, 24)) == date(2026, 9, 23)


def test_lineage_sources_line():
    from src.reports.lineage import SECTION_SOURCES, SOURCES, sources_line

    assert set(SECTION_SOURCES) == set(SECTION_CATALOG)
    assert all(src == "ai" or src in SOURCES for srcs in SECTION_SOURCES.values() for src in srcs)
    line = sources_line(["top", "sales", "chats", "team_updates"], "2026-09-22")
    assert line.startswith("Hurtownia dash, IdeaERP, WhatsApp, Slack, MyBed Group OS") and line.endswith("liczby do 22.09")
