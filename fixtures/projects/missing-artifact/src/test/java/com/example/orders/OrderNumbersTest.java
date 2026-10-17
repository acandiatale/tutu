package com.example.orders;

import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class OrderNumbersTest {

    @Test
    public void prefixesBranch() {
        assertTrue(new OrderNumbers("seoul").next().startsWith("ORD-SEOUL-"));
        assertTrue(new OrderNumbers(null).next().startsWith("ORD-HQ-"));
    }
}
