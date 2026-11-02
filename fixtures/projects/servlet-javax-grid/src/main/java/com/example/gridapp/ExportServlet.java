package com.example.gridapp;

import java.io.IOException;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.example.grid.GridExporter;

/** Hands the request to the vendor grid component, whose API takes the servlet request / response types. */
public class ExportServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    private final GridExporter exporter = new GridExporter();

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        exporter.export(req, resp);
    }
}
