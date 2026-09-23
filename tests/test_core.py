"""Unit tests for pure logic: rendering, auth, profiles, dash math, OS views, chat parsing."""

from datetime import date, timedelta

from src.common.timeutil import today
from src.connectors.dash import build_overview
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
    assert all(not s["enabled"] for s in mb["sections"][1:])
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
    ads = [{"date": ref.isoformat(), "platform": "meta", "spend": 100, "conversion_value": 500, "clicks": 10}]
    rooms = [{"showroom": "warszawa", "date": ref.isoformat(), "orders": 2, "revenue_pln": 5000},
             {"showroom": "krakow", "date": "2026-12-04", "orders": 1, "revenue_pln": 999}]  # future row ignored
    o = build_overview(rows, ads, rooms, ref)
    assert o["total"]["revenue"] == 1100
    assert o["total"]["vs_prev_day_pct"] == 10.0
    assert o["marketing"]["mer_day"] == 11.0
    assert o["marketing"]["platforms"][0]["roas"] == 5.0
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
