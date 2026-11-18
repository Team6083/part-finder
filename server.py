from __future__ import annotations

import hmac
import logging
import os
from typing import Annotated, List
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints, create_model

from cache import ResultCache
from parsers import set_allowed_hosts
from rules import UnsupportedInput, allowed_hosts, build_rules, resolve

log = logging.getLogger(__name__)

MAX_QUERY_LEN = 2048
DEFAULT_MAX_BATCH = 50


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


QueryStr = Annotated[str, StringConstraints(max_length=MAX_QUERY_LEN)]


def _public_error(error: str) -> str:
    """Fixed client-facing message; the real text stays in the server log."""
    low = error.lower()
    if "not found" in low or "no variants" in low or "no json-ld" in low or "mismatch" in low:
        return "not found"
    return "upstream error"


def create_app(cache: ResultCache | None = None, ttl: float | None = None,
               map_ttl: float | None = None, api_key: str | None = None,
               max_batch: int | None = None) -> FastAPI:
    app = FastAPI(
        title="Part Finder",
        description="Robotics parts price and stock lookup.",
        version="0.1.0",
    )
    api_key = api_key if api_key is not None else os.environ.get("PART_FINDER_API_KEY") or None
    if max_batch is None:
        max_batch = int(os.environ.get("PART_FINDER_MAX_BATCH", DEFAULT_MAX_BATCH))
    if max_batch < 1:
        raise ValueError(f"PART_FINDER_MAX_BATCH must be >= 1, got {max_batch}")
    BatchRequest = create_model("BatchRequest", queries=(List[QueryStr], Field(max_length=max_batch)))
    rules = build_rules(cache, ttl, map_ttl)
    # Only store hosts may be fetched, including every redirect hop.
    set_allowed_hosts(allowed_hosts(rules))

    def require_key(x_api_key: str | None = Header(None)):
        if api_key and not hmac.compare_digest((x_api_key or "").encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    def _lookup(q: str) -> list[dict]:
        try:
            search_fn, query = resolve(q, rules, allow_generic=False)
            result, error = search_fn(query)
        except UnsupportedInput:
            return [{"status": "error", "url": q, "error": "unsupported input"}]
        except Exception:
            log.exception("lookup crashed for %r", q)
            return [{"status": "error", "url": q, "error": "upstream error"}]
        if error:
            log.warning("lookup failed for %r: %s", q, error)
            return [{"status": "error", "url": q, "error": _public_error(error)}]
        if isinstance(result, list):
            return [{"status": "ok", **r} for r in result]
        return [{"status": "ok", **result}]

    router = APIRouter(dependencies=[Depends(require_key)])

    @router.get("/lookup", response_model=List[LookupResult])
    def lookup(q: str = Query(..., max_length=MAX_QUERY_LEN, description="SKU or product URL")):
        """Look up a part by SKU or URL. Returns all variants if a URL is given."""
        return _lookup(q)

    def lookup_batch(req):
        """Look up multiple parts. Results are flattened into a single list."""
        return [item for q in req.queries for item in _lookup(q)]

    # Set explicitly: the model is built per app, so a string annotation could not resolve it.
    lookup_batch.__annotations__["req"] = BatchRequest
    router.post("/lookup", response_model=List[LookupResult])(lookup_batch)

    app.include_router(router, prefix="/api/v1")
    # Unversioned paths kept for existing callers.
    app.include_router(router, deprecated=True)

    return app
