package com.example.legacy.util;

import static org.junit.Assert.assertEquals;

import java.io.StringReader;
import java.util.Arrays;
import java.util.Date;
import java.util.Map;

import org.junit.Test;

public class LedgerUtilsTest {

    @Test
    public void codecRoundTrip() throws Exception {
        String encoded = Codec.encode("ledger-2010");
        assertEquals("bGVkZ2VyLTIwMTA=", encoded.trim());
        assertEquals("ledger-2010", Codec.decode(encoded));
    }

    @Test
    public void isoRoundTrip() {
        Date date = new Date(1262304000000L);
        assertEquals(date, Dates.fromIso(Dates.toIso(date)));
    }

    @Test
    public void totalsAndOrder() throws Exception {
        LedgerLines lines = new LedgerLines();
        Map<String, Long> totals = lines.totals(new StringReader("# header\nA;10\nB;30\nA;5\n\n"));
        assertEquals(Long.valueOf(15L), totals.get("A"));
        assertEquals(Arrays.asList("B", "A"), lines.accountsByTotal(totals));
    }
}
