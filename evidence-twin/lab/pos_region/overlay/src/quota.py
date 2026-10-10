"""Per-tenant request quota. The tenant-to-region map is deploy configuration
(TENANT_REGION), not part of this repository."""

import importlib
import os


def limit_for(tenant: str) -> int:
    region = os.environ["TENANT_REGION"]
    return importlib.import_module(f"src.regions.{region}").LIMIT
