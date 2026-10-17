package com.example.orders.web;

import java.math.BigDecimal;
import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import com.example.orders.domain.PurchaseOrder;
import com.example.orders.service.OrderService;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    public record PlaceOrder(String customer, BigDecimal amount) {
    }

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public PurchaseOrder place(@RequestBody PlaceOrder cmd) {
        return orderService.place(cmd.customer(), cmd.amount());
    }

    @PostMapping("/{id}/pay")
    public PurchaseOrder pay(@PathVariable long id) {
        return orderService.pay(id);
    }

    @GetMapping("/{id}")
    public PurchaseOrder get(@PathVariable long id) {
        return orderService.get(id);
    }

    @GetMapping
    public List<PurchaseOrder> byCustomer(@RequestParam String customer) {
        return orderService.byCustomer(customer);
    }
}
