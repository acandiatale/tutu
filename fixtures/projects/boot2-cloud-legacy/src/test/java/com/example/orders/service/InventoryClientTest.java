package com.example.orders.service;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class InventoryClientTest {

    @Test
    void stockIsDerivedFromTheOrderId() {
        assertEquals(21, new InventoryClient().stockFor("order-1"));
        assertEquals(0, new InventoryClient().stockFallback("order-1"));
    }
}
