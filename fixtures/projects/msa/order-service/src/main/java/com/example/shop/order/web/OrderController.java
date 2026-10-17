package com.example.shop.order.web;

import java.util.List;

import javax.validation.Valid;

import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import com.example.shop.order.model.OrderLine;
import com.example.shop.order.model.OrderView;
import com.example.shop.order.service.OrderService;

import lombok.RequiredArgsConstructor;

@RestController
@RequiredArgsConstructor
public class OrderController {

    private final OrderService orderService;

    @PostMapping("/api/users/{userId}/orders")
    public OrderView place(@PathVariable long userId, @Valid @RequestBody List<@Valid OrderLine> lines) {
        return orderService.place(userId, lines);
    }
}
