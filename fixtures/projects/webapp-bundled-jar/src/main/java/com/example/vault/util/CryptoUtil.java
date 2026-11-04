package com.example.vault.util;

import com.acme.seed.Seed;

/** Wraps the vendor SEED cipher that ships as WEB-INF/lib/acme-seed-1.0.jar (declared by no pom). */
public final class CryptoUtil {

    private CryptoUtil() {
    }

    public static String encrypt(String plain) {
        return Seed.encrypt(plain == null ? "" : plain);
    }
}
