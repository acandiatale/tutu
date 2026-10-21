package com.example.shop.service;

public class GreetingService {

    public String greet(String name) {
        return "Hello, " + (name == null || name.trim().isEmpty() ? "guest" : name.trim()) + "!";
    }
}
