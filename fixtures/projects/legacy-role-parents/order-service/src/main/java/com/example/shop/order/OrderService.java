package com.example.shop.order;

import com.example.shop.catalog.CatalogService;
import com.example.shop.catalog.Item;
import org.springframework.stereotype.Service;

@Service
public class OrderService {

    private final CatalogService catalog;

    public OrderService(CatalogService catalog) {
        this.catalog = catalog;
    }

    public String summary() {
        int total = 0;
        for (Item item : catalog.items()) {
            total += item.getPriceCents();
        }
        return catalog.items().size() + " items, total " + total + " cents";
    }
}
