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
from src.reports.render import clean_stored_html, email_html, split_lead, to_html


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


def test_session_tokens_roundtrip_and_tamper(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
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
    assert f"## {SECTION_CATALOG['slack']['label']} —" not in prompt  # disabled by default


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
    assert line.startswith("Hurtownia dash, IdeaERP, WhatsApp, MyBed Group OS, Slack") and line.endswith("liczby do 22.09")


def test_numeric_marking_is_idempotent_and_counts_cells_with_attributes():
    from src.reports.render import _mark_numeric_columns

    html = to_html("| Sklep | Przychód | Komentarz |\n|---|---:|---|\n| mybed.pl | 209 049 zł | Dobry dzień |\n"
                   "| mybed.de | 60 859 zł | 2 (rabat dla hotelu jutro) |")
    assert _mark_numeric_columns(html) == html
    assert '<th>Komentarz</th>' in html and '<td>Dobry dzień</td>' in html
    assert to_html('## A {: onclick="x()" }').count("onclick=") == 1  # stays text, not an attribute
    assert "<h2>" in to_html('## A {: onclick="x()" }')


def test_group_reports_threads():
    from datetime import datetime, timezone

    from src.processing.team_updates import group_reports

    def m(i, uid, ts, reply=None):
        return {"id": i, "timestamp": datetime(2026, 9, 24, 14, i, tzinfo=timezone.utc), "body": f"b{i}",
                "metadata": {"user_id": uid, "user_name": uid, "ts": ts, "is_reply": reply is not None,
                             "thread_ts": reply}}

    msgs = [m(1, "KAMILA", "100.1"), m(2, "CEO", "100.2", reply="100.1"), m(3, "KAMILA", "100.3", reply="100.1"),
            m(4, "SZYMON", "100.4", reply="100.1")]
    g = group_reports(msgs)
    assert set(g) == {"KAMILA"}  # commenters never get a "report" of their own
    assert [x["id"] for x in g["KAMILA"]] == [1, 2, 3, 4]


def test_slack_clean_text_names_are_not_regex_templates():
    import asyncio

    from src.connectors.slack_api import SlackAPI

    api = SlackAPI(token="x")
    api._users["U1"] = {"id": "U1", "name": "Jan \\Kowalski \\g<0>", "email": "", "bot": False}
    out = asyncio.run(api.clean_text("hej <@U1> zobacz <https://a.pl|link>"))
    assert out == "hej @Jan \\Kowalski \\g<0> zobacz link (https://a.pl)"


def test_slack_chunks_closing_fence_on_boundary():
    from src.outputs.slack_output import _chunks

    body = "x" * 3790 + "\n```\n" + "y\n" * 10
    text = "```\n" + body[:3790 - 4] + "\n```\n\n• dalej\n" + "z" * 100
    for size in range(3780, 3800):
        for part in _chunks(text, size):
            assert not part.startswith("```\n```"), size
            assert part.count("```") % 2 == 0


def test_kpi_missing_source_shows_no_data_instead_of_zero():
    from src.reports.kpi import headline_cards, kpi_data

    ref = date(2026, 9, 22)
    rows = [{"date": (ref - timedelta(days=i)).isoformat(), "source_shop": "mybed.pl", "orders_count": 5,
             "revenue_gross_pln": 1000, "revenue_paid_pln": 0, "avg_order_value_pln": 0,
             "revenue_gross_original": 0, "original_currency": "PLN"} for i in range(1, 60)]  # nothing for ref
    o = build_overview(rows, [], [], ref, missing={"sales", "meta", "google"})
    data = kpi_data(o, {"enabled": True, "periods": ["1"], "metrics": ["revenue", "spend_total"], "per_shop": False})
    cards = headline_cards(data)
    assert [c["value"] for c in cards] == ["—", "—"] and not any(c["has_pct"] for c in cards)
    assert "Brak danych za 22.09" in data["note"]


def test_suggestion_key_is_content_based():
    from src.processing.team_updates import suggestion_key

    assert suggestion_key("os_update", {"task_id": "t1", "suggested_status": "Done"}) == "os_update:t1:Done"
    assert suggestion_key("not_in_os", {"title": "  Wyłączenie  widoku "}) == "not_in_os:wyłączenie widoku"


def test_sanitizer_keeps_list_numbering_and_drops_everything_else():
    html = to_html("1. A\n2. B\n\nkomentarz\n\n3. C\n\n```{ .fixed #x }\nkod\n```\n\n![x](http://evil/p.png)")
    assert '<ol start="3">' in html and "<img" not in html and "fixed" not in html
    mail = email_html(label="R", title="T", body_html=html, url=None, footer="f")
    assert 'start="3"' in mail


def test_slack_edit_hash_and_similarity_rules():
    from src.processing.team_updates import _similar, normalize_pushed

    assert _similar("Wyłączenie widoku showroomów", "wyłączenie widoku  showroomów")
    assert not _similar("Oferta dla Hotelu Arłamów", "Oferta dla Hotelu Gołębiewski")
    data = normalize_pushed({"pushed": ["not_in_os:0"], "not_in_os": [{"title": "Abc"}]})
    assert data["pushed_keys"] == ["not_in_os:abc"]


def test_stored_html_from_old_reports_is_sanitised_on_display():
    legacy = ('<pre class="fixed inset-0" id="scroller"><code>x</code></pre><p><img src="https://e.x/p.png">'
              '<a href="javascript:alert(1)" onclick="x">a</a></p><ol start="3"><li>c</li></ol>')
    out = clean_stored_html(legacy)
    assert "img" not in out and "fixed" not in out and "javascript" not in out and "onclick" not in out
    assert '<ol start="3">' in out
    fresh = to_html("| Sklep | Przychód |\n|---|---:|\n| mybed.pl | 12 000 zł |")
    assert clean_stored_html(fresh) == fresh


def _os_daily_fixture():
    from src.processing.os_daily import DailyData

    def rep(day, pid, status="submitted", **kw):
        return {"id": f"du-{day}-{pid}", "date": day, "authorId": pid, "status": status, "items": [], "needs": [],
                "plan": [], "suggestions": [], "analysis": {"status": "done", "runs": 1, "summary": ""}, **kw}

    anna = rep("2026-09-28", "p-anna", items=[
        {"kind": "task", "taskId": "t1", "title": "stary tytuł", "state": "done", "note": "wysłane"},
        {"kind": "task", "taskId": "t2", "title": "Newsletter", "state": "waiting", "note": ""},
        {"kind": "text", "title": "Telefon do drukarni", "state": "progress", "note": ""}],
        extra="Cennik B2B\n", needs=[{"personId": "p-marek", "text": "akcept banerów do środy", "taskId": "t1"}],
        plan=[{"kind": "task", "taskId": "t2", "title": "x"}], offReason=None,
        comments=[{"text": "prywatna rozmowa"}], reopenedAt="2026-09-28T15:00:00Z",
        suggestions=[{"status": "applied", "action": "set_status", "targetTitle": "Banery", "from": {"status": "Doing"},
                      "to": {"status": "Review"}},
                     {"status": "rejected", "action": "create_task", "task": {"title": "odrzucone"}},
                     {"status": "pending", "action": "comment", "targetTitle": "X"}],
        submittedAt="2026-09-28T14:21:07.000Z",
        analysis={"status": "done", "runs": 1, "summary": "Zamknięte banery."})
    rows = [
        rep("2026-09-24", "p-anna", items=[{"taskId": "t2", "state": "waiting"}],
            needs=[{"personId": "p-marek", "text": "akcept banerów BF", "taskId": "t1"}]),
        rep("2026-09-25", "p-anna", items=[{"taskId": "t2", "state": "waiting"}],
            needs=[{"personId": "p-marek", "text": "akcept banerów", "taskId": "t1"}]),
        anna,
        rep("2026-09-28", "p-ola", status="off", offReason="choroba"),
        rep("2026-09-28", "p-jan", status="draft", metrics={"firstInputAt": "2026-09-28T10:00:00Z"}),
    ]
    return DailyData(rows=rows, settings={"enabled": True, "participantIds": ["p-anna", "p-ola", "p-jan", "p-ewa"]},
                     people={p: {"id": p, "name": n} for p, n in [("p-anna", "Anna A"), ("p-ola", "Ola O"),
                                                                  ("p-jan", "Jan J"), ("p-ewa", "Ewa E"),
                                                                  ("p-marek", "Marek M")]},
                     tasks={"t1": {"id": "t1", "title": "Banery BF"}, "t2": {"id": "t2", "title": "Newsletter CZ"}},
                     blockers=[{"id": "b1", "title": "Brak akceptu", "status": "Open", "ownerId": "p-marek",
                                "sourceDailyId": "du-2026-09-23-p-anna", "createdAt": "2026-09-23T10:00:00.000Z"},
                               {"id": "b2", "title": "Nie z raportu", "status": "Open",
                                "createdAt": "2026-09-01T10:00:00.000Z"}],
                     start=date(2026, 9, 7), end=date(2026, 9, 28))


def test_os_daily_report_view_hides_private_fields():
    from src.processing.os_daily import report_view

    dd = _os_daily_fixture()
    v = report_view(dd.for_day(date(2026, 9, 28))[0], dd)
    d = v["data"]
    assert v["person_name"] == "Anna A" and v["link"].endswith("/daily?r=du-2026-09-28-p-anna")
    assert d["done"] == ["Banery BF — wysłane"]  # current task title, not the copy
    assert d["waiting"] == ["Newsletter CZ"] and d["in_progress"] == ["Telefon do drukarni (poza OS)"]
    assert d["outside_os"] == ["Cennik B2B"] and d["outside_os_items"] == ["Telefon do drukarni"]
    assert d["needs"] == ["Marek M: akcept banerów do środy"] and d["next"] == ["Newsletter CZ"]
    assert d["applied"] == ["Banery: Doing → Review"] and d["pending_suggestions"] == 1
    assert d["plan_quality"] == "clear" and d["edited_after_submit"] and d["summary"] == "Zamknięte banery."
    flat = str(v)
    assert "odrzucone" not in flat and "prywatna rozmowa" not in flat and "choroba" not in flat


def test_os_daily_missing_rule_and_escalations():
    from datetime import datetime

    from src.common.timeutil import WARSAW
    from src.processing.os_daily import escalations, missing_for

    dd = _os_daily_fixture()
    day = date(2026, 9, 28)  # Monday
    status, missing, not_yet = missing_for(day, dd, datetime(2026, 9, 28, 18, 0, tzinfo=WARSAW))
    assert status == "day_in_progress" and not missing
    assert [(m["name"], m["state"]) for m in not_yet] == [("Ewa E", "brak"), ("Jan J", "szkic")]
    status, missing, _ = missing_for(day, dd, datetime(2026, 9, 29, 10, 5, tzinfo=WARSAW))
    assert status == "ok" and [m["name"] for m in missing] == ["Ewa E", "Jan J"]  # off = excused, draft = missing
    assert missing_for(date(2026, 9, 27), dd)[0] == "weekend"
    assert missing_for(date(2026, 11, 11), dd, datetime(2026, 11, 12, 12, 0, tzinfo=WARSAW))[0] == "weekend"

    now_ = datetime(2026, 9, 29, 10, 5, tzinfo=WARSAW)
    texts = [e["text"] for e in escalations(day, dd, now_, os_from=date(2026, 9, 24))]
    assert any("czeka na Marek M" in t and "od 2026-09-24" in t for t in texts)  # same request for 2 workdays
    assert any("Newsletter CZ" in t and "3 raportów" in t for t in texts)  # waiting 3 reports in a row
    assert any("Brak akceptu" in t for t in texts) and not any("Nie z raportu" in t for t in texts)
    missing_streak = [e for e in escalations(day, dd, now_, os_from=date(2026, 9, 24)) if e["type"] == "missing"]
    assert {e["person"] for e in missing_streak} == {"Ewa E", "Jan J"}  # Thu, Fri, Mon without a report
    assert not [e for e in escalations(day, dd, now_, os_from=date.max) if e["type"] == "missing"]


def test_workdays_follow_polish_holidays():
    from src.common.timeutil import is_workday, previous_workday

    assert not is_workday(date(2026, 11, 11)) and not is_workday(date(2026, 6, 4))  # Independence Day, Corpus Christi
    assert previous_workday(date(2026, 11, 12)) == date(2026, 11, 10)
