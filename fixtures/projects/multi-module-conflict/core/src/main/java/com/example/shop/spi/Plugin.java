package com.example.shop.spi;

/** Extension point, implementations registered in META-INF/services/com.example.shop.spi.Plugin. */
public interface Plugin {

    String name();
}
