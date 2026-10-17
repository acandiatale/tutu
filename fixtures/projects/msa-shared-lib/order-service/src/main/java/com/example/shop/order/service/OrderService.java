package com.example.shop.order.service;

import java.util.List;
import java.util.Map;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestTemplate;

import com.example.shop.common.TraceIds;
import com.example.shop.order.model.OrderView;

import lombok.extern.slf4j.Slf4j;

@Slf4j
@Service
public class OrderService {

    private final RestTemplate userService;

    public OrderService(@Qualifier("userServiceRestTemplate") RestTemplate userService) {
        this.userService = userService;
    }

    public OrderView place(long userId, List<OrderView.Line> lines) {
        return OrderView.builder()
                .orderId(TraceIds.newId("ord"))
                .userName(userName(userId))
                .lines(lines)
                .build();
    }

    @SuppressWarnings("unchecked")
    String userName(long userId) {
        try {
            Map<String, Object> user = userService.getForObject("/api/users/{id}", Map.class, userId);
            return user == null ? "unknown" : String.valueOf(user.get("name"));
        } catch (RestClientException e) {
            log.warn("user-service unavailable: {}", e.getMessage());
            return "unknown";
        }
    }
}
