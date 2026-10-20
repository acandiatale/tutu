package com.example.orders.service;

import org.springframework.stereotype.Service;

import com.netflix.hystrix.contrib.javanica.annotation.HystrixCommand;
import com.netflix.hystrix.contrib.javanica.annotation.HystrixProperty;

/** Calls the (fictional) inventory service; Hystrix wraps the calls with fallbacks. */
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

    /** Named command with Hystrix-only tuning (timeout, thread pool) that has no Resilience4j annotation twin. */
    @HystrixCommand(commandKey = "inventoryReserve", groupKey = "inventory", threadPoolKey = "inventoryPool",
            fallbackMethod = "reserveFallback",
            commandProperties = {
                    @HystrixProperty(name = "execution.isolation.thread.timeoutInMilliseconds", value = "2000")
            },
            ignoreExceptions = IllegalStateException.class)
    public boolean reserve(String orderId, int quantity) {
        if (quantity <= 0) {
            throw new IllegalStateException("quantity must be positive");
        }
        return orderId != null && quantity < 100;
    }

    public boolean reserveFallback(String orderId, int quantity) {
        return false;
    }
}
