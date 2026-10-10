"""Per-tenant request quota."""

import importlib

from src.tenants import TENANTS


def limit_for(tenant: str) -> int:
    region = f"r{TENANTS.index(tenant) + 1}"
    return importlib.import_module(f"src.regions.{region}").LIMIT
