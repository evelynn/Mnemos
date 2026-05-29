// Reference-system TypeScript BFF — the analyzer-benchmark fixture.
//
// This is a deliberately small but *realistically shaped* slice of a
// polyglot system: a TypeScript front-of-house that calls a C# API
// over HTTP and also touches the database directly through an ORM and
// raw SQL. The companion ground_truth.json labels every contract /
// data entity that genuinely exists here, so the benchmark can score
// the analyzer's precision / recall against a known answer.

import { prisma } from "./db";

export interface Order {
  id: number;
  total: number;
}

export class OrderService {
  // Static string-literal fetch — the analyzer resolves this to
  // http.GET./api/orders.
  async listOrders(): Promise<Order[]> {
    const res = await fetch("/api/orders");
    return res.json();
  }

  // Numeric literal path segment — normalises to /api/orders/{id},
  // which is the same canonical id the C# [HttpGet("/api/orders/{id}")]
  // controller produces. This is the cross-language join.
  async getOrder(): Promise<Order> {
    const res = await fetch("/api/orders/42");
    return res.json();
  }

  // Non-GET verb pulled from the options object.
  async createCustomer(): Promise<void> {
    await fetch("/api/customers", { method: "POST" });
  }

  // ORM read — Prisma client.<model>.<verb>().
  async cachedOrders(): Promise<Order[]> {
    return prisma.order.findMany();
  }

  // ORM write.
  async registerCustomer(): Promise<void> {
    await prisma.customer.create({ data: {} });
  }

  // Raw SQL — the analyzer extracts the table from the FROM clause.
  async auditTrail(db: any): Promise<unknown> {
    return db.query("SELECT action FROM order_audit WHERE ok = 1");
  }

  // Dynamic (template-literal) URL — a static analyzer genuinely
  // cannot resolve this. ground_truth.json lists the endpoint anyway
  // so the benchmark's recall figure reflects the real limitation
  // rather than a curated best case.
  async getOrderLines(id: string): Promise<unknown> {
    const res = await fetch(`/api/orders/${id}/lines`);
    return res.json();
  }
}
