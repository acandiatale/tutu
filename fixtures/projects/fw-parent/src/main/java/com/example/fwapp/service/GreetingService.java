package com.example.fwapp.service;

import com.example.fw.core.FwResult;
import com.example.fw.core.FwStrings;

public class GreetingService {

    public FwResult<String> greet(String name) {
        if (name != null && name.length() > 64) {
            return FwResult.fail("TOO_LONG");
        }
        return FwResult.ok("Hello " + FwStrings.defaultIfBlank(name, "guest"));
    }
}
