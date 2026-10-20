package com.example.shop.core;

import java.util.ResourceBundle;

import org.apache.commons.lang3.StringUtils;
import org.springframework.stereotype.Service;

@Service
public class GreetingService {

    public String greet(String name) {
        String greeting = ResourceBundle.getBundle("messages").getString("greeting");
        return greeting + ", " + StringUtils.defaultIfBlank(name, "guest");
    }
}
