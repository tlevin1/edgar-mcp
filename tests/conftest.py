"""Synthetic companyfacts payloads shaped exactly like EDGAR's, small enough to
reason about by hand. No network."""

import pytest

from edgar_mcp.sec import Company

ACCN = "0000000001-26-000001"
PRIOR_ACCN = "0000000001-25-000001"
Q_ACCN = "0000000001-26-000002"
END, PRIOR_END = "2025-12-31", "2024-12-31"
FY_START = "2025-01-01"


def row(val, end, accn=ACCN, start=None, form="10-K", fy=2025, fp="FY", filed="2026-02-15"):
    r = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start:
        r["start"] = start
    return r


def payload(concepts: dict[str, list[dict]], unit_overrides: dict[str, str] | None = None) -> dict:
    unit_overrides = unit_overrides or {}
    return {
        "cik": 1,
        "entityName": "Test Co",
        "facts": {
            "us-gaap": {
                name: {"label": name, "description": f"{name} description", "units": {unit_overrides.get(name, "USD"): rows}}
                for name, rows in concepts.items()
            }
        },
    }


def clean_10k() -> dict[str, list[dict]]:
    """A 10-K where every identity holds. Includes prior-year comparatives,
    as real filings do."""
    d = lambda v, start=FY_START: row(v, END, start=start)  # noqa: E731
    return {
        "Assets": [row(1000, END), row(900, PRIOR_END)],
        "Liabilities": [row(600, END), row(550, PRIOR_END)],
        "StockholdersEquity": [row(400, END), row(350, PRIOR_END)],
        "LiabilitiesAndStockholdersEquity": [row(1000, END), row(900, PRIOR_END)],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [d(500), row(450, PRIOR_END, start="2024-01-01")],
        "CostOfGoodsAndServicesSold": [d(300)],
        "GrossProfit": [d(200)],
        "OperatingIncomeLoss": [d(120)],
        "NetIncomeLoss": [d(90)],
        "NetCashProvidedByUsedInOperatingActivities": [d(150)],
        "NetCashProvidedByUsedInInvestingActivities": [d(-60)],
        "NetCashProvidedByUsedInFinancingActivities": [d(-50)],
        "PaymentsToAcquirePropertyPlantAndEquipment": [d(40)],
        "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": [d(-5)],
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect": [d(35)],
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": [row(135, END), row(100, PRIOR_END)],
        "CashAndCashEquivalentsAtCarryingValue": [row(130, END)],
    }


class FakeSec:
    def __init__(self, facts_by_cik: dict[int, dict]):
        self.facts_by_cik = facts_by_cik

    def companies(self):
        return [Company(1, "TEST", "Test Co"), Company(2, "OTHR", "Other Test Holdings")]

    def company_facts(self, cik):
        return self.facts_by_cik[cik]


@pytest.fixture
def clean_facts():
    return payload(clean_10k())
