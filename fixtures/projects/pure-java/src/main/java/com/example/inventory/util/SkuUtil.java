package com.example.inventory.util;

import org.apache.commons.lang3.StringUtils;

import com.google.common.base.CharMatcher;
import com.google.common.base.Strings;

public final class SkuUtil {

    private SkuUtil() {
    }

    /** Normalize to "ABC-000123". */
    public static String normalize(String raw) {
        String cleaned = CharMatcher.javaLetterOrDigit().retainFrom(StringUtils.defaultString(raw)).toUpperCase();
        String letters = CharMatcher.javaLetter().retainFrom(cleaned);
        String digits = CharMatcher.javaDigit().retainFrom(cleaned);
        return StringUtils.left(letters, 3) + "-" + Strings.padStart(digits, 6, '0');
    }
}
