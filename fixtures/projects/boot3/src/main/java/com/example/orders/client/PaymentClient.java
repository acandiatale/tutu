package com.example.orders.client;

import java.math.BigDecimal;
import java.util.Map;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;

@Component
public class PaymentClient {

    private final RestTemplate restTemplate;

    public PaymentClient(@Qualifier("paymentRestTemplate") RestTemplate restTemplate) {
        this.restTemplate = restTemplate;
    }

    @SuppressWarnings("unchecked")
    public String charge(long orderId, BigDecimal amount) {
        Map<String, Object> body = restTemplate.postForObject("/charges",
                Map.of("orderId", orderId, "amount", amount), Map.class);
        return body == null ? null : String.valueOf(body.get("chargeId"));
    }
}
