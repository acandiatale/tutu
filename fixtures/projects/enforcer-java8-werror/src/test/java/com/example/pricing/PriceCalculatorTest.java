package com.example.pricing;

import static org.junit.Assert.assertEquals;

import java.math.BigDecimal;

import org.junit.Test;

public class PriceCalculatorTest {

    @Test
    public void bestRuleWins() {
        PriceCalculator calc = new PriceCalculator()
                .addRule(new PriceRule(10, 5))
                .addRule(new PriceRule(100, 20));
        assertEquals(new BigDecimal("960.00"), calc.total(new BigDecimal("10"), 120));
        assertEquals(new BigDecimal("47.50"), calc.total(new BigDecimal("5"), 10));
        assertEquals(new BigDecimal("9.00"), calc.total(new BigDecimal("3"), 3));
    }
}
