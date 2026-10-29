package com.example.util;

/**
 * Source of example-util-1.0.jar (WebContent/WEB-INF/lib), kept for reference only: neither build.xml nor the
 * synthesized pom compiles this directory. Rebuild the jar with `python3 make-stubs.py --util` (javac --release 8).
 */
public class Greeter {

    private final String salutation;

    public Greeter(String salutation) {
        this.salutation = salutation;
    }

    public String greet(String name) {
        return salutation + ", " + name + "!";
    }
}
