package com.example.inventory.util;

import java.text.ParseException;
import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Date;
import java.util.TimeZone;

/**
 * Legacy date helpers (java.util.Date / SimpleDateFormat). Kept for ERP compatibility.
 */
public final class DateUtil {

    public static final String ERP_PATTERN = "yyyyMMddHHmmss";

    private DateUtil() {
    }

    public static String format(Date date) {
        if (date == null) {
            return "";
        }
        SimpleDateFormat fmt = new SimpleDateFormat(ERP_PATTERN);
        fmt.setTimeZone(TimeZone.getTimeZone("Asia/Seoul"));
        return fmt.format(date);
    }

    public static Date parse(String text) {
        SimpleDateFormat fmt = new SimpleDateFormat(ERP_PATTERN);
        fmt.setTimeZone(TimeZone.getTimeZone("Asia/Seoul"));
        fmt.setLenient(false);
        try {
            return fmt.parse(text);
        } catch (ParseException e) {
            throw new IllegalArgumentException("not an ERP timestamp: " + text, e);
        }
    }

    public static Date startOfDay(Date date) {
        Calendar cal = Calendar.getInstance(TimeZone.getTimeZone("Asia/Seoul"));
        cal.setTime(date);
        cal.set(Calendar.HOUR_OF_DAY, 0);
        cal.set(Calendar.MINUTE, 0);
        cal.set(Calendar.SECOND, 0);
        cal.set(Calendar.MILLISECOND, 0);
        return cal.getTime();
    }
}
