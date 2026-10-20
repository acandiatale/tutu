package com.example.legacy.util;

import java.util.Calendar;
import java.util.Date;
import java.util.TimeZone;

import javax.xml.bind.DatatypeConverter;

/**
 * ISO-8601 timestamps for the ledger files (JAXB's DatatypeConverter was the Java 6 way to do it).
 */
public final class Dates {

    private Dates() {
    }

    public static String toIso(Date date) {
        Calendar cal = Calendar.getInstance(TimeZone.getTimeZone("UTC"));
        cal.setTime(date);
        return DatatypeConverter.printDateTime(cal);
    }

    public static Date fromIso(String text) {
        return DatatypeConverter.parseDateTime(text).getTime();
    }
}
