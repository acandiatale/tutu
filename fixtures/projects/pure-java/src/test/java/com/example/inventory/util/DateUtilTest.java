package com.example.inventory.util;

import static org.junit.Assert.assertEquals;

import java.util.Date;

import org.junit.Test;

public class DateUtilTest {

    @Test
    public void roundTrip() {
        Date d = DateUtil.parse("20240102030405");
        assertEquals("20240102030405", DateUtil.format(d));
    }

    @Test
    public void skuNormalize() {
        assertEquals("ABC-000012", SkuUtil.normalize("abc 12"));
    }
}
