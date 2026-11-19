package com.example.fwapp;

import com.example.fw.core.FwStrings;

/** Builds on Java 8; System.runFinalizersOnExit is gone since Java 11, so the converted tree does not compile. */
public final class App {

    private App() {
    }

    @SuppressWarnings("deprecation")
    public static void main(String[] args) {
        System.runFinalizersOnExit(true);
        System.out.println(FwStrings.defaultIfBlank(args.length > 0 ? args[0] : null, "hello"));
    }
}
