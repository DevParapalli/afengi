"""API tools for the example agent -- one function per supported workflow.

Tool docstrings are the model-facing schema: ADK builds each tool's description
from the hints and docstring, and the framework prompt tells the agent to treat
that description as the source of truth for mandatory fields. They stay long on
purpose; everything else here is one line.
"""

from framework import make_authenticated_request

from . import config


def _call(endpoint: str, body: dict, *, connection: str = "api") -> dict:
    """Authenticated POST to `<connection>_connection`'s endpoint."""
    # connection names a config field, so one agent can reach several APIs each
    # with its own credentials. Empty values are stripped, not sent as nulls.
    conn = getattr(config.settings, f"{connection}_connection")
    url = f"{conn.url}/{endpoint}"
    payload = {k: v for k, v in body.items() if v not in (None, "", [], {})}
    return make_authenticated_request(url, conn, method="POST", json_body=payload)


def order_status(order_id: str) -> dict:
    """Category 'Orders', core issue 'Status' -- look up one order.

    Fetches the current status, line items and fulfilment history for a single
    order. Use when the request asks where an order is, whether it shipped, or why
    it has not been fulfilled.

    Args:
        order_id: The order identifier, e.g. 'ORD-118422'. Required.
    """
    return _call("orders/status", {"orderId": order_id})


def order_refund_status(order_id: str, line_number: str = "") -> dict:
    """Category 'Orders', core issue 'Refund' -- check refund progress.

    Returns any refund raised against an order, with its state and settlement
    date. Use for "where is my refund" style requests.

    Args:
        order_id: The order identifier, e.g. 'ORD-118422'. Required.
        line_number: Restrict to a single order line. Optional.
    """
    return _call("orders/refund", {"orderId": order_id, "lineNumber": line_number})


def inventory_check(sku: str, location: str = "") -> dict:
    """Category 'Inventory', core issue 'Availability' -- stock for one SKU.

    Returns on-hand and committed quantities for a SKU, optionally narrowed to one
    location. Use when the request is about stock levels or backorders.

    Args:
        sku: The stock-keeping unit, e.g. 'SKU-4471-A'. Required.
        location: Warehouse or region code to restrict the lookup to. Optional.
    """
    return _call("inventory/check", {"sku": sku, "location": location})


def account_lookup(account_id: str = "", email: str = "") -> dict:
    """Category 'Accounts', core issue 'Lookup' -- resolve a customer account.

    Returns the account record for either an account id or a registered email
    address. Supply at least one of the two; if the request has neither, ask for
    one rather than guessing.

    Args:
        account_id: The account identifier, e.g. 'ACC-90210'. Optional if email is given.
        email: The account's registered email address. Optional if account_id is given.
    """
    return _call("accounts/lookup", {"accountId": account_id, "email": email})
