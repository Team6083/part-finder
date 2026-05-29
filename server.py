from __future__ import annotations

from typing import List
from fastapi import FastAPI, Query
from pydantic import BaseModel

from cache import ResultCache
from rules import build_rules, resolve


class LookupResult(BaseModel):
    status: str
    name: str = ""
    sku: str = ""
    price: str = ""
    in_stock: bool | None = None
    stock_status: str = ""
    source: str = ""
    url: str = ""
    error: str = ""


class BatchRequest(BaseModel):
    queries: List[str]


def create_app(cache: ResultCache | None = None, ttl: float | None = None) -> FastAPI:
    app = FastAPI(
        title="Part Finder",
        description="Robotics parts price and stock lookup.",
        version="0.1.0",
    )
    rules = build_rules(cache, ttl)

    def _lookup(q: str) -> list[dict]:
        search_fn, query = resolve(q, rules)
        result, error = search_fn(query)
        if error:
            return [{"status": "error", "url": q, "error": error}]
        if isinstance(result, list):
            return [{"status": "ok", **r} for r in result]
        return [{"status": "ok", **result}]

    @app.get("/lookup", response_model=List[LookupResult])
    def lookup(q: str = Query(..., description="SKU or product URL")):
        """Look up a part by SKU or URL. Returns all variants if a URL is given."""
        return _lookup(q)

    @app.post("/lookup", response_model=List[LookupResult])
    def lookup_batch(req: BatchRequest):
        """Look up multiple parts. Results are flattened into a single list."""
        return [item for q in req.queries for item in _lookup(q)]

    return app
