package com.example.orders;

class OrdersRepo {

    Order findById(Integer id) {
        return load(id);
    }

    Order save(Order order) {
        return persist(order);
    }

    private Order load(Integer id) {
        return null;
    }

    private Order persist(Order order) {
        return order;
    }
}
