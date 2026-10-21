package com.example.shop.catalog;

import lombok.AllArgsConstructor;
import lombok.Getter;

@Getter
@AllArgsConstructor
public class Item {
    private final String sku;
    private final String name;
    private final int priceCents;
}
