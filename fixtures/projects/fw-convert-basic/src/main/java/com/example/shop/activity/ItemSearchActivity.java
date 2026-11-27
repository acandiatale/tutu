package com.example.shop.activity;

import com.example.fw.activity.ExampleMyBatisSearch;

/** Project-own activity: the framework's search with a project hook (the service XML gives it a sql-key). */
public class ItemSearchActivity extends ExampleMyBatisSearch {
    @Override
    protected String sqlKey(String configured) {
        return configured;
    }
}
