"""CFO Studio tools in the Agents MCP — one connector in Claude for everything.

Each tool forwards to the same tool of the CFO Studio MCP server (src/connectors/cfo_studio.py) with the app API key, so the
logic, the numbers and the permissions stay in CFO Studio (a "viewer" key gets no bank data, invoices or SQL — those tools then
answer with CFO Studio's refusal). Results come back as CFO Studio returns them: JSON, columnar for tables ({columns, rows}).
"""

from __future__ import annotations

from src.connectors.cfo_studio import CFOStudioClient, CFOStudioError


async def _forward(tool: str, args: dict) -> str:
    try:
        return await CFOStudioClient().call(tool, {k: v for k, v in args.items() if v is not None})
    except CFOStudioError as e:
        return str(e)


def register(mcp) -> None:
    @mcp.tool()
    async def cfo_guide() -> str:
        """Finanse Delty (CFO Studio): definicje (wynik realny, COGS należny, Marża I, prognoza), zasady czytania i opis
        narzędzi cfo_*. Przeczytaj przed analizą finansów."""
        return await _forward("cfo_guide", {})

    @mcp.tool()
    async def cfo_overview() -> str:
        """Finanse Delty dziś (CFO Studio): 6 zamkniętych miesięcy (wynik realny vs Incro, marże, 3M), YTD, prognoza
        bieżącego miesiąca, przychód MTD vs M-1 i rok temu, koszt w produkcji, świeżość danych, alerty. Tylko Delta
        Industries (MyBed) — bez MittoHome i NOMO."""
        return await _forward("cfo_overview", {})

    @mcp.tool()
    async def cfo_list_datasets(with_columns: bool = True) -> str:
        """Zestawy danych CFO Studio (wynik, Incro, COGS, transport, marketing, płatności, kontrole, porządki, bank) z opisem
        i kolumnami — do cfo_get_dataset."""
        return await _forward("list_datasets", {"with_columns": with_columns})

    @mcp.tool()
    async def cfo_get_dataset(
        dataset: str,
        date_from: str | None = None,
        date_to: str | None = None,
        filters: list[dict] | None = None,
        columns: list[str] | None = None,
        order_by: str | None = None,
        desc: bool | None = None,
        limit: int | None = None,
    ) -> str:
        """Wiersze zestawu CFO Studio. dataset: nazwa z cfo_list_datasets (np. pnl_bridge, incro_pnl, cogs_nalezny,
        transport_nalezny, marketing, double_invoices). date_from / date_to: YYYY-MM-DD albo YYYY-MM po kolumnie daty
        zestawu. filters: [{column, op, value}], op: =, <>, >, >=, <, <=, in, like, contains, is_null, not_null.
        Wynik kolumnowy {columns, rows}."""
        return await _forward("get_dataset", {"dataset": dataset, "from": date_from, "to": date_to, "filters": filters,
                                              "columns": columns, "order_by": order_by, "desc": desc, "limit": limit})

    @mcp.tool()
    async def cfo_forecast(as_of: str | None = None) -> str:
        """Pełna prognoza wyniku realnego miesiąca z CFO Studio: składowe MTD / reszta / prognoza (przychód, COGS,
        transport, kanały, marketing, baza stała, gotówka, podatek; koszty dodatnie) i metoda. as_of: dzień (YYYY-MM-DD),
        domyślnie wczoraj; wcześniejsza data = backtest."""
        return await _forward("get_forecast", {"as_of": as_of})

    @mcp.tool()
    async def cfo_order(order_id: str) -> str:
        """Jedno zamówienie w CFO Studio: ERP, pozycje, koszt produkcji (panel / cennik), transport, FV sprzedaży i
        korekty, panel producenta. order_id: Shoper63493-1, ZAM/03074 albo sam numer Shoper."""
        return await _forward("get_order", {"order_id": order_id})

    @mcp.tool()
    async def cfo_invoices(
        q: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        direction: str | None = None,
        account: str | None = None,
        kind: str | None = None,
        min_net_pln: float | None = None,
        limit: int | None = None,
    ) -> str:
        """Faktury z Fakturowni w CFO Studio (PLN i EUR, kwoty *_pln po NBP). direction: sale | cost, account: pln | eur,
        kind: vat | correction | …, q: numer, kontrahent, NIP albo opis; daty sprzedaży YYYY-MM-DD."""
        return await _forward("search_invoices", {"q": q, "from": date_from, "to": date_to, "direction": direction,
                                                  "account": account, "kind": kind, "min_net_pln": min_net_pln, "limit": limit})

    @mcp.tool()
    async def cfo_describe(name: str) -> str:
        """Kolumny i definicja tabeli / widoku CFO Studio (np. finance.v_cfo_pnl_bridge) — przed cfo_sql."""
        return await _forward("describe_object", {"name": name})

    @mcp.tool()
    async def cfo_sql(sql: str, limit: int | None = None) -> str:
        """Zapytanie SELECT / WITH tylko do odczytu na bazie CFO Studio (schematy finance, erp, panel, cennik): jedna
        instrukcja, limit wierszy, timeout. Agreguj w SQL. Zamówienia Shopify… i NOMO-… to nie Delta — wykluczaj."""
        return await _forward("query_sql", {"sql": sql, "limit": limit})
