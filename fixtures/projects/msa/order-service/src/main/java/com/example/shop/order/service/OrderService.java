package com.example.shop.order.service;

import java.util.List;
import java.util.UUID;

import org.springframework.stereotype.Service;

import com.example.shop.order.client.UserClient;
import com.example.shop.order.model.OrderLine;
import com.example.shop.order.model.OrderView;

import lombok.RequiredArgsConstructor;

@Service
@RequiredArgsConstructor
public class OrderService {

    private final UserClient userClient;

    public OrderView place(long userId, List<OrderLine> lines) {
        return OrderView.builder()
                .orderId(UUID.randomUUID().toString())
                .userName(userClient.userName(userId))
                .lines(lines)
                .build();
    }
}
