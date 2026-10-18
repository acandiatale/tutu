package com.example.orders.service;

import org.springframework.stereotype.Service;

import com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand;

/** Calls the (fictional) inventory service; Hystrix wraps the call with a fallback. */
@Service
public class InventoryClient {

    @HystrixCommand(fallbackMethod = "stockFallback")
    public int stockFor(String orderId) {
        if (orderId == null || orderId.isEmpty()) {
            throw new IllegalArgumentException("order id required");
        }
        return orderId.length() * 3;
    }

    public int stockFallback(String orderId) {
        return 0;
    }
}
