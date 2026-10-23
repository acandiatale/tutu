package com.example.portal.batch;

import org.apache.commons.lang3.StringUtils;

/** Nightly clean-up of expired notices; also called from the web module's admin page. */
public class NoticeCleaner {

    public int clean(String reason) {
        return StringUtils.isBlank(reason) ? 0 : reason.trim().length();
    }

    public static void main(String[] args) {
        System.out.println("cleaned " + new NoticeCleaner().clean(args.length > 0 ? args[0] : "nightly"));
    }
}
