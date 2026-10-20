package com.example.stock.service;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class StockServiceTest {

    private final StockService service = new StockService(new InventoryGateway());

    @Test
    void knownSkuReturnsStock() {
        assertEquals(12, service.stockFor("A-100"));
    }

    @Test
    void failingCallFallsBackToZero() {
        assertEquals(0, service.stockFor("UNKNOWN"));
    }
}
