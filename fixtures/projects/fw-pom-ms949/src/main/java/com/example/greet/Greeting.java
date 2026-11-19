package com.example.greet;

import com.example.fw.core.FwStrings;

/** Builds on Java 8 and 21; UpgradeToJava21 replaces the deprecated wrapper constructor. */
public final class Greeting {

    private Greeting() {
    }

    @SuppressWarnings("deprecation")
    public static Integer count() {
        return new Integer(5);
    }

    public static String text(String name) {
        return FwStrings.defaultIfBlank(name, "hello") + " x" + count();
    }
}
