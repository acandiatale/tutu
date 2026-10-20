package com.example.shop.shared;

/** Text helpers shared by the shop modules. */
public final class Shared {

    private Shared() {
    }

    public static String greet(String name) {
        return "hello, " + name;
    }

    // added in the web copy only (the core copy was never updated)
    public static String farewell(String name) {
        return "bye, " + name;
    }
}
