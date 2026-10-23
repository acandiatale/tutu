package com.example.legacy.service;

import java.util.Arrays;
import java.util.List;

public class ReportService {

    public List<String> titles() {
        return Arrays.asList("daily", "weekly", "monthly");
    }
}
