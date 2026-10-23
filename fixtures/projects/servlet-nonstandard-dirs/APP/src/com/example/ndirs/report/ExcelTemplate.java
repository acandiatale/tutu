package com.example.ndirs.report;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;

/** Reads the Excel XML template that STATIC/resources packages onto the classpath. */
public class ExcelTemplate {

    public static final String PATH = "templates/report-template.xml";

    public String render(String rows) {
        try (InputStream in = ExcelTemplate.class.getClassLoader().getResourceAsStream(PATH)) {
            if (in == null) {
                throw new IllegalStateException("missing classpath resource " + PATH);
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            return new String(out.toByteArray(), StandardCharsets.UTF_8).replace("${rows}", rows);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }
}
