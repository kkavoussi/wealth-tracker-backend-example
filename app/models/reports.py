"""
Pydantic response models for report endpoints.
"""
from pydantic import BaseModel
from typing import Any, Optional


class BalanceSheetResponse(BaseModel):
    as_of: str
    net_worth: float
    total_assets: float
    total_liabilities: float
    assets: dict[str, Any]
    liabilities: dict[str, Any]


class PeriodInfo(BaseModel):
    period: str
    start_date: str
    end_date: str


class IncomeStatementResponse(BaseModel):
    period: str
    start_date: str
    end_date: str
    summary: dict[str, float]
    income: dict[str, Any]
    expenses: dict[str, Any]
    transfers: dict[str, Any]
    payroll: Optional[dict[str, Any]] = None  # None when no payroll connection exists


class CashFlowResponse(BaseModel):
    period: str
    start_date: str
    end_date: str
    summary: dict[str, float]
    net_income_breakdown: dict[str, float]
    operating: dict[str, Any]
    investing: dict[str, Any]
    financing: dict[str, Any]
