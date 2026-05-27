"""Accuracy fixture for ggoss-py — mirrors the TS fixture so a
cross-language accuracy comparison is meaningful. Same domain
(orders), same shape (interface/class/functions), so a regression
in either analyzer's accuracy is comparable apples-to-apples."""

from dataclasses import dataclass


@dataclass
class Order:
    id: str
    customer_id: str
    total: float
    status: str  # "pending" | "confirmed" | "cancelled"


class OrdersRepo:
    def __init__(self) -> None:
        self._rows: list[Order] = []

    def add(self, order: Order) -> None:
        self._rows.append(order)

    def get_by_id(self, oid: str) -> Order | None:
        for o in self._rows:
            if o.id == oid:
                return o
        return None

    def cancel(self, oid: str) -> bool:
        o = self.get_by_id(oid)
        if o is None:
            return False
        o.status = "cancelled"
        return True


def create_order(repo: OrdersRepo, oid: str, customer_id: str, total: float) -> Order:
    order = Order(id=oid, customer_id=customer_id, total=total, status="pending")
    repo.add(order)
    return order


def confirm_order(repo: OrdersRepo, oid: str) -> bool:
    o = repo.get_by_id(oid)
    if o is None:
        return False
    o.status = "confirmed"
    return True


async def cancel_and_notify(repo: OrdersRepo, oid: str) -> bool:
    """Async function — separate symbol kind. Calls into the repo
    just like the sync versions; the analyzer must track callees
    here too (regression risk: easy to miss AsyncFunctionDef)."""
    return repo.cancel(oid)
