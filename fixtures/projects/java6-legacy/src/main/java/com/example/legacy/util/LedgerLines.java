package com.example.legacy.util;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.Reader;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.apache.commons.lang3.StringUtils;

/**
 * Parses "account;amount" lines. Pre-Java-7 style on purpose: no diamond, no try-with-resources,
 * anonymous classes instead of lambdas.
 */
public class LedgerLines {

    public Map<String, Long> totals(Reader in) throws IOException {
        Map<String, Long> totals = new HashMap<String, Long>();
        BufferedReader reader = new BufferedReader(in);
        try {
            String line;
            while ((line = reader.readLine()) != null) {
                if (StringUtils.isBlank(line) || line.startsWith("#")) {
                    continue;
                }
                String[] parts = StringUtils.split(line, ';');
                String account = StringUtils.trim(parts[0]);
                long amount = Long.parseLong(StringUtils.trim(parts[1]));
                Long current = totals.get(account);
                totals.put(account, Long.valueOf(current == null ? amount : current.longValue() + amount));
            }
        } finally {
            reader.close();
        }
        return totals;
    }

    public List<String> accountsByTotal(final Map<String, Long> totals) {
        List<String> accounts = new ArrayList<String>(totals.keySet());
        Collections.sort(accounts, new Comparator<String>() {
            public int compare(String a, String b) {
                return totals.get(b).compareTo(totals.get(a));
            }
        });
        return accounts;
    }
}
