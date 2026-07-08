// Accuracy fixture for ggoss-kotlin — orders domain (Spring). Mirrors the
// py/ts/java fixtures. Update this file and expected.json together.
package com.example.orders

import org.springframework.web.bind.annotation.*

@RestController
@RequestMapping("/orders")
class OrdersController(private val repo: OrdersRepo) {

    @GetMapping("/{id}")
    fun getById(@PathVariable(name = "id") id: Int): Order {
        return repo.findById(id)
    }

    @PostMapping
    fun create(@RequestBody order: Order): Order {
        validate(order)
        return repo.save(order)
    }

    private fun validate(order: Order) {
        checkOrder(order)
    }
}

class OrdersRepo {
    fun findById(id: Int): Order {
        return load(id)
    }

    fun save(order: Order): Order {
        return persist(order)
    }

    private fun load(id: Int): Order = Order(id)

    private fun persist(order: Order): Order = order
}
