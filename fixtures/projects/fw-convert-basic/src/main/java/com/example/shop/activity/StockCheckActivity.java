package com.example.shop.activity;

import java.util.Map;

/** Project-own activity with Java code only: no database, no sql-key. */
public class StockCheckActivity {
    public int execute(Map<String, Object> params) {
        Object qty = params.get("qty");
        return qty == null ? 0 : Integer.parseInt(String.valueOf(qty)) > 0 ? 1 : 0;
    }
}
