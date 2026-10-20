package com.example.stock.service;

import java.util.Map;

import org.springframework.stereotype.Component;

/** Stand-in for a remote inventory system: unknown SKUs fail like a remote call would. */
@Component
public class InventoryGateway {

    private final Map<String, Integer> stock = Map.of("A-100", 12, "B-200", 0);

    public int available(String sku) {
        Integer n = stock.get(sku);
        if (n == null) {
            throw new IllegalStateException("inventory system has no record for " + sku);
        }
        return n;
    }
}
