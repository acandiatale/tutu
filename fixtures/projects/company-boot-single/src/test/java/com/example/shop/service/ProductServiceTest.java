package com.example.shop.service;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class ProductServiceTest {

    @Test
    void priceUsesTheCurrency() {
        assertEquals("5000 EUR", new ProductService("EUR").price("mouse"));
    }
}
