package com.example.shop.service;

import org.springframework.stereotype.Service;

@Service
public class GreetingService {

    public String greet(String name) {
        return "Welcome to the shop, " + (name == null || name.isEmpty() ? "guest" : name) + "!";
    }
}
