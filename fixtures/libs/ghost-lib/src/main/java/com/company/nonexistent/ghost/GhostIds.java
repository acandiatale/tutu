package com.company.nonexistent.ghost;

/** Id generator from a library that only ever lived in a decommissioned company repository. */
public final class GhostIds {

    private GhostIds() {
    }

    public static String next(String prefix) {
        return prefix + "-" + Long.toString(System.nanoTime(), 36);
    }
}
