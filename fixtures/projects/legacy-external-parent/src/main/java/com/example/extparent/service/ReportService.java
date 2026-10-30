package com.example.extparent.service;

import java.io.IOException;
import java.io.StringWriter;

import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVPrinter;
import org.apache.commons.text.StringEscapeUtils;
import org.springframework.stereotype.Service;

/** Uses the two libraries whose versions the old in-house parent managed (commons-text, commons-csv). */
@Service
public class ReportService {

    public String escape(String raw) {
        return StringEscapeUtils.escapeHtml4(raw);
    }

    public String csvLine(String... cells) throws IOException {
        StringWriter out = new StringWriter();
        try (CSVPrinter printer = new CSVPrinter(out, CSVFormat.DEFAULT)) {
            printer.printRecord((Object[]) cells);
        }
        return out.toString().trim();
    }
}
