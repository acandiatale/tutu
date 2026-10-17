package com.example.inventory.service;

import java.util.Collection;
import java.util.Date;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import org.apache.commons.lang3.StringUtils;

import com.example.inventory.model.StockItem;
import com.google.common.base.Preconditions;
import com.google.common.collect.ImmutableList;
import com.google.common.collect.Maps;

/**
 * In-memory inventory with simple reservation semantics.
 */
public class InventoryService {

    private final Map<String, StockItem> items = Maps.newLinkedHashMap();

    public StockItem register(String sku, String name, int quantity) {
        Preconditions.checkArgument(StringUtils.isNotBlank(sku), "sku must not be blank");
        Preconditions.checkArgument(quantity >= 0, "quantity must be >= 0");
        StockItem item = new StockItem(StringUtils.upperCase(sku.trim()), name, quantity, new Date());
        items.put(item.getSku(), item);
        return item;
    }

    public StockItem reserve(String sku, int amount) {
        StockItem item = find(sku);
        if (item.getQuantity() < amount) {
            throw new IllegalStateException("insufficient stock for " + sku + ": " + item.getQuantity() + " < " + amount);
        }
        item.setQuantity(item.getQuantity() - amount);
        item.setLastUpdated(new Date());
        return item;
    }

    public StockItem find(String sku) {
        StockItem item = items.get(StringUtils.upperCase(StringUtils.trimToEmpty(sku)));
        if (item == null) {
            throw new IllegalArgumentException("unknown sku " + sku);
        }
        return item;
    }

    public List<StockItem> lowStock(int threshold) {
        return ImmutableList.copyOf(items.values().stream()
                .filter(i -> i.getQuantity() <= threshold)
                .collect(Collectors.toList()));
    }

    public Collection<StockItem> all() {
        return ImmutableList.copyOf(items.values());
    }
}
