// Accuracy fixture for ggoss-java — orders domain (Spring). Mirrors the
// py/ts fixtures. Update this file and expected.json together.
package com.example.orders;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/orders")
class OrdersController {

    private final OrdersRepo repo;

    OrdersController(OrdersRepo repo) {
        this.repo = repo;
    }

    @GetMapping("/{id}")
    Order getById(@PathVariable(name = "id") Integer id) {
        return repo.findById(id);
    }

    @PostMapping
    Order create(@RequestBody Order order) {
        validate(order);
        return repo.save(order);
    }

    private void validate(Order order) {
        requireNonNull(order);
    }
}
