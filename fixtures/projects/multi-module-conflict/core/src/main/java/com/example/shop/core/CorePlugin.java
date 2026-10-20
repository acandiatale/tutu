package com.example.shop.core;

import com.example.shop.spi.Plugin;

public class CorePlugin implements Plugin {

    @Override
    public String name() {
        return "core";
    }
}
