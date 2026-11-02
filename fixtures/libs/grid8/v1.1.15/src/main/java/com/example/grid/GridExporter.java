package com.example.grid;

import java.io.IOException;
import java.io.PrintWriter;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/**
 * Vendor grid export entry point, version 1.1.15: its public API takes {@code javax.servlet} request / response types.
 */
public class GridExporter {

    public static final String VERSION = "1.1.15";

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
