/* Accuracy fixture for ggoss-cpp — orders domain, mirrors the py/ts
 * fixtures so cross-language accuracy comparisons stay meaningful.
 * Update this file and expected.json together. */
#include "orders.h"

struct order {
    int id;
    int status;
};

enum order_status {
    ORDER_PENDING,
    ORDER_CONFIRMED,
    ORDER_CANCELLED
};

static int validate_order(struct order *o) {
    return o->id > 0;
}

int order_add(struct order *o) {
    if (!validate_order(o)) {
        return -1;
    }
    return persist_order(o); /* extern — defined outside the fixture */
}

int create_order(int id) {
    struct order o;
    o.id = id;
    o.status = ORDER_PENDING;
    return order_add(&o);
}

void cancel_order(struct order *o) {
    validate_order(o);
    o->status = ORDER_CANCELLED;
}
