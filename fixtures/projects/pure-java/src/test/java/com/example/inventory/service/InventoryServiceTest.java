package com.example.inventory.service;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Before;
import org.junit.Test;

public class InventoryServiceTest {

    private InventoryService service;

    @Before
    public void setUp() {
        service = new InventoryService();
        service.register("abc-1", "Widget", 10);
    }

    @Test
    public void reserveDecrementsQuantity() {
        assertEquals(7, service.reserve("ABC-1", 3).getQuantity());
    }

    @Test(expected = IllegalStateException.class)
    public void reserveFailsWhenInsufficient() {
        service.reserve("abc-1", 11);
    }

    @Test
    public void lowStockFilters() {
        assertTrue(service.lowStock(5).isEmpty());
    }
}
