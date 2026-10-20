package com.example.legacy.util;

import java.io.IOException;
import java.io.UnsupportedEncodingException;

import sun.misc.BASE64Decoder;
import sun.misc.BASE64Encoder;

/**
 * Base64 helpers for the ledger export files (written for Java 6, which has no public Base64 API).
 */
public final class Codec {

    private static final String CHARSET = "UTF-8";

    private Codec() {
    }

    public static String encode(String text) {
        if (text == null) {
            return null;
        }
        try {
            return new BASE64Encoder().encode(text.getBytes(CHARSET));
        } catch (UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
    }

    public static String decode(String base64) throws IOException {
        if (base64 == null) {
            return null;
        }
        byte[] raw = new BASE64Decoder().decodeBuffer(base64);
        return new String(raw, CHARSET);
    }
}
