package com.example.ndirs.web;

import com.example.ndirs.report.ExcelTemplate;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import java.io.IOException;

/** Mapped in STATIC/WEB-INF/web.xml (no annotation); renders the report page from the classpath template. */
public class ReportServlet extends HttpServlet {

    private final ExcelTemplate template = new ExcelTemplate();

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        req.setAttribute("rows", template.render("<Row/>"));
        req.getRequestDispatcher("/WEB-INF/jsp/report.jsp").forward(req, resp);
    }
}
