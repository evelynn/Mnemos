// Realistic shape for a small TS order-processing module — the
// kind of code Mnemos will see in production. Used by
// scripts/accuracy/measure.py to check the ggoss-ts analyzer
// extracts every symbol & call we expect.

export interface Order {
  id: string;
  customerId: string;
  total: number;
  status: "pending" | "confirmed" | "cancelled";
}

export type OrderId = string;

export class OrdersRepo {
  private rows: Order[] = [];

  add(order: Order): void {
    this.rows.push(order);
  }

  getById(id: OrderId): Order | undefined {
    return this.rows.find((o) => o.id === id);
  }

  cancel(id: OrderId): boolean {
    const o = this.getById(id);
    if (!o) return false;
    o.status = "cancelled";
    return true;
  }
}

export function createOrder(repo: OrdersRepo, payload: Omit<Order, "status">): Order {
  const order: Order = { ...payload, status: "pending" };
  repo.add(order);
  return order;
}

export const confirmOrder = (repo: OrdersRepo, id: OrderId): boolean => {
  const o = repo.getById(id);
  if (!o) return false;
  o.status = "confirmed";
  return true;
};
