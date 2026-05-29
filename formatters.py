STOCK_LABEL = {
    "in_stock":     "In Stock",
    "out_of_stock": "Out of Stock",
    "preorder":     "Pre-order",
    "unknown":      "Unknown",
}

TSV_HEADER = "\t".join(["status", "name", "sku", "price", "stock", "source", "url"])


def format_tsv(result, error, display_url: str) -> str:
    if error:
        return "\t".join(["ERROR", "", "", "", "", "", display_url])
    return "\t".join([
        "OK", result["name"], result["sku"], result["price"],
        result["stock_status"], result["source"], result["url"],
    ])


def format_human(result, error, query: str) -> str:
    sep = "─" * 48
    if error:
        return f"{sep}\n  {query}\n  ERROR  {error}\n"
    stock = STOCK_LABEL.get(result["stock_status"], result["stock_status"])
    price = f"${result['price']}" if result["price"] else "—"
    return (
        f"{sep}\n"
        f"  {result['name']}\n"
        f"  SKU    {result['sku']}\n"
        f"  Price  {price}\n"
        f"  Stock  {stock}\n"
        f"  URL    {result['url']}\n"
    )
