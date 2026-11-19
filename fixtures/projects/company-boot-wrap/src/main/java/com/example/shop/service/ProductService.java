package com.example.shop.service;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

@Service
public class ProductService {

    private final String currency;

    public ProductService(@Value("${shop.currency:KRW}") String currency) {
        this.currency = currency;
    }

    public List<String> names() {
        return Collections.unmodifiableList(Arrays.asList("keyboard", "mouse", "monitor"));
    }

    public String price(String name) {
        return name.length() * 1000 + " " + currency;
    }
}
