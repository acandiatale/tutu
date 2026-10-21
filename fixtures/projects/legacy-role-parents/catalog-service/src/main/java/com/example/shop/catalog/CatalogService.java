package com.example.shop.catalog;

import java.util.Arrays;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class CatalogService {

    public List<Item> items() {
        return Arrays.asList(new Item("A-1", "mug", 1200), new Item("B-2", "pen", 300));
    }
}
