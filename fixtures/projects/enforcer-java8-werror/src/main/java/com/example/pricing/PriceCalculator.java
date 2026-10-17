package com.example.pricing;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

import org.apache.commons.lang3.Validate;

/** Applies the best matching {@link PriceRule} to an order line. */
public class PriceCalculator {

    private final List<PriceRule> rules = new ArrayList<>();

    public PriceCalculator addRule(PriceRule rule) {
        rules.add(Validate.notNull(rule, "rule"));
        Collections.sort(rules, Comparator.comparingInt(PriceRule::getMinQuantity).reversed());
        return this;
    }

    public BigDecimal total(BigDecimal unitPrice, int quantity) {
        Validate.isTrue(quantity > 0, "quantity must be positive: %d", quantity);
        BigDecimal unit = unitPrice;
        for (PriceRule rule : rules) {
            if (rule.appliesTo(quantity)) {
                unit = rule.apply(unitPrice);
                break;
            }
        }
        return unit.multiply(BigDecimal.valueOf(quantity)).setScale(2, RoundingMode.HALF_UP);
    }

    /** Legacy cleanup hook: Object.finalize() is deprecated for removal since JDK 18 → [removal] warning on 21. */
    @Override
    protected void finalize() throws Throwable {
        rules.clear();
        super.finalize();
    }
}
