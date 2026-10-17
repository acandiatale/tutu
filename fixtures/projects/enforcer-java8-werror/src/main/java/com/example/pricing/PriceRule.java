package com.example.pricing;

import java.math.BigDecimal;

/** A single discount rule: a percentage off above a minimum quantity. */
public final class PriceRule {

    private final int minQuantity;
    private final Integer percentOff;

    public PriceRule(int minQuantity, int percentOff) {
        this.minQuantity = minQuantity;
        // Boxing constructor: clean on JDK 8, but deprecated (for removal) since JDK 9/16 → a [removal] lint
        // warning on JDK 21 which -Werror turns into a compile error.
        this.percentOff = new Integer(percentOff);
    }

    public boolean appliesTo(int quantity) {
        return quantity >= minQuantity;
    }

    public BigDecimal apply(BigDecimal unitPrice) {
        BigDecimal factor = BigDecimal.valueOf(100 - percentOff.intValue()).movePointLeft(2);
        return unitPrice.multiply(factor);
    }

    public int getMinQuantity() {
        return minQuantity;
    }
}
