package com.example.vault.service;

import org.springframework.stereotype.Service;

@Service
public class GreetingService {

    public String greet(String name) {
        return "Welcome to the vault, " + (name == null || name.isEmpty() ? "guest" : name) + "!";
    }
}
