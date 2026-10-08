"""Exercise the tools through a real MCP client against an in-memory server."""

import asyncio

from fastmcp import Client

from edgar_mcp.server import create_server

from .conftest import ACCN, FakeSec, clean_10k, payload


def call(server, tool, args, raise_on_error=True):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(tool, args, raise_on_error=raise_on_error)

    return asyncio.run(go())


def server(facts=None):
    return create_server(FakeSec({1: facts or payload(clean_10k()), 2: payload({})}))


def test_tools_are_registered_read_only():
    async def go():
        async with Client(server()) as c:
            return await c.list_tools()

    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == {"lookup_company", "list_filings", "get_financials", "get_concept", "check_tieouts"}
    assert all(t.annotations.read_only_hint for t in tools.values())


def test_lookup_puts_exact_ticker_first():
    r = call(server(), "lookup_company", {"query": "test"})
    names = [c["ticker"] for c in r.structured_content["result"]]
    assert names == ["TEST", "OTHR"]


def test_check_tieouts_end_to_end():
    r = call(server(), "check_tieouts", {"ticker": "test"})
    assert r.structured_content["filing"]["accn"] == ACCN
    assert r.structured_content["summary"]["pass"] == 5


def test_unknown_ticker_is_a_tool_error_not_a_crash():
    r = call(server(), "check_tieouts", {"ticker": "NOPE"}, raise_on_error=False)
    assert r.is_error and "lookup_company" in r.content[0].text


def test_company_without_filings_explains_itself():
    r = call(server(), "check_tieouts", {"ticker": "OTHR"}, raise_on_error=False)
    assert r.is_error and "predecessor" in r.content[0].text


def test_unknown_accession_is_rejected():
    r = call(server(), "check_tieouts", {"ticker": "TEST", "accession": "bogus"}, raise_on_error=False)
    assert r.is_error and "list_filings" in r.content[0].text


def test_get_concept_suggests_close_matches():
    r = call(server(), "get_concept", {"ticker": "TEST", "concept": "GrossProfits"}, raise_on_error=False)
    assert r.is_error and "GrossProfit" in r.content[0].text


def test_get_concept_returns_newest_first():
    r = call(server(), "get_concept", {"ticker": "TEST", "concept": "Assets"})
    ends = [v["end"] for v in r.structured_content["values"]]
    assert ends == sorted(ends, reverse=True)
