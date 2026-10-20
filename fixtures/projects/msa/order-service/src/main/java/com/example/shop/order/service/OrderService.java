package com.example.shop.order.service;

import java.util.List;
import java.util.UUID;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import com.example.shop.order.client.UserClient;
import com.example.shop.order.model.OrderLine;
import com.example.shop.order.model.OrderView;

import lombok.RequiredArgsConstructor;

@Service
@RequiredArgsConstructor
public class OrderService {

    private final UserClient userClient;

    // supplied per environment (-D / env / external config at deploy time): no packaged default
    @Value("${order.tax-rate}")
    private String taxRate;

    @Value("${shop.region:kr}")
    private String region;

    public OrderView place(long userId, List<OrderLine> lines) {
        return OrderView.builder()
                .orderId(UUID.randomUUID().toString())
                .userName(userClient.userName(userId))
                .lines(lines)
                .build();
    }

    public String pricing() {
        return region + "/" + taxRate;
    }
}
