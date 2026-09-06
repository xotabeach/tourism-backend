"""Public shape of the company-details singleton (mobile "О приложении")."""

from __future__ import annotations

from pydantic import BaseModel


class CompanyDetailsOut(BaseModel):
    legal_name: str
    brand_name: str
    inn: str
    ogrn: str
    address: str
    email: str
    phone: str
    telegram: str
    working_hours: str
