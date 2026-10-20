package com.example.shop.web;

import com.example.shop.spi.Plugin;

public class WebPlugin implements Plugin {

    @Override
    public String name() {
        return "web";
    }
}
