package com.example.orders;

import org.apache.commons.lang3.StringUtils;

import com.company.nonexistent.ghost.GhostIds;

/** Order numbers: upper-case branch code + ghost-lib id. */
public class OrderNumbers {

    private final String branch;

    public OrderNumbers(String branch) {
        this.branch = StringUtils.upperCase(StringUtils.defaultIfBlank(branch, "hq"));
    }

    public String next() {
        return GhostIds.next("ORD-" + branch);
    }
}
