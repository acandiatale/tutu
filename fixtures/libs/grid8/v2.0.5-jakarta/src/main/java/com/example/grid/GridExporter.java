package com.example.grid;

import java.io.IOException;
import java.io.PrintWriter;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * Vendor grid export entry point, version 2.0.5-jakarta: its public API takes {@code jakarta.servlet} request / response types.
 */
public class GridExporter {

    public static final String VERSION = "2.0.5-jakarta";

    /** Writes the grid rows of the request's {@code sheet} parameter as CSV to the response. */
    public void export(HttpServletRequest request, HttpServletResponse response) throws IOException {
        String sheet = request.getParameter("sheet");
        response.setContentType("text/csv");
        PrintWriter out = response.getWriter();
        out.println("sheet,version");
        out.println((sheet == null ? "default" : sheet) + "," + VERSION);
        out.flush();
    }

    public String version() {
        return VERSION;
    }
}
