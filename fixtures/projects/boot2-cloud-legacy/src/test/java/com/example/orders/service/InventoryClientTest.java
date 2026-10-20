package com.example.orders.service;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class InventoryClientTest {

    @Test
    void stockIsDerivedFromTheOrderId() {
        assertEquals(21, new InventoryClient().stockFor("order-1"));
        assertEquals(0, new InventoryClient().stockFallback("order-1"));
        assertEquals(true, new InventoryClient().reserve("order-1", 2));
        assertEquals(false, new InventoryClient().reserveFallback("order-1", 2));
    }
}
