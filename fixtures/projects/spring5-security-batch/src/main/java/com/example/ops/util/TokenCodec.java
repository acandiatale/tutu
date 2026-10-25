package com.example.ops.util;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

import sun.misc.BASE64Decoder;
import sun.misc.BASE64Encoder;

/**
 * Base64 for the download tokens (written before java.util.Base64 was allowed here). sun.misc.BASE64* no longer
 * compile on JDK 21: the handler jdk-base64-shim points them at generated sbmig.compat classes.
 */
public final class TokenCodec {

    private TokenCodec() {
    }

    public static String encode(String token) {
        return new BASE64Encoder().encode(token.getBytes(StandardCharsets.UTF_8));
    }

    public static String decode(String encoded) {
        try {
            return new String(new BASE64Decoder().decodeBuffer(encoded), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalArgumentException("bad token", e);
        }
    }
}
