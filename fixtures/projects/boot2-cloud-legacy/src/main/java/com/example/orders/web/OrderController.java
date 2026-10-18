package com.example.orders.web;

import java.util.List;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.example.orders.service.InventoryClient;

import io.swagger.annotations.Api;
import io.swagger.annotations.ApiOperation;
import io.swagger.annotations.ApiParam;

@RestController
@RequestMapping("/api/orders")
@Api(tags = "orders")
public class OrderController {

    private final InventoryClient inventory;

    public OrderController(InventoryClient inventory) {
        this.inventory = inventory;
    }

    @GetMapping
    @ApiOperation(value = "List orders", notes = "Returns the open orders")
    public List<String> list() {
        return List.of("order-1", "order-2");
    }

    @GetMapping("/{id}/stock")
    @ApiOperation(value = "Stock for an order line")
    public int stock(@ApiParam(value = "order id", required = true) @PathVariable("id") String id) {
        return inventory.stockFor(id);
    }
}
