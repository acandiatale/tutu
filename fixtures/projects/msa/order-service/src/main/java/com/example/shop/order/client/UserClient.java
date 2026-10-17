package com.example.shop.order.client;

import java.util.Map;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestTemplate;

import lombok.extern.slf4j.Slf4j;

@Slf4j
@Component
public class UserClient {

    private final RestTemplate restTemplate;

    public UserClient(@Qualifier("userServiceRestTemplate") RestTemplate restTemplate) {
        this.restTemplate = restTemplate;
    }

    @SuppressWarnings("unchecked")
    public String userName(long userId) {
        try {
            Map<String, Object> user = restTemplate.getForObject("/api/users/{id}", Map.class, userId);
            return user == null ? "unknown" : String.valueOf(user.get("name"));
        } catch (RestClientException e) {
            log.warn("user-service unavailable: {}", e.getMessage());
            return "unknown";
        }
    }
}
