package com.example.legacy.web;

import java.io.IOException;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.example.legacy.service.ReportService;

/** Front controller of the old days: every *.do and /front/* request ends up here (mapped in web.xml only). */
public class FrontServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    private final ReportService reportService = new ReportService();

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        String view = getInitParameter("defaultView");
        req.setAttribute("titles", reportService.titles());
        req.getRequestDispatcher(view != null ? view : "/WEB-INF/jsp/list.jsp").forward(req, resp);
    }
}
