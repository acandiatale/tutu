package com.example.legacy.web;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;

/** Declared in web.xml without a servlet-mapping: only reached through RequestDispatcher by name. */
public class ReportServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    @Override
    public void init() throws ServletException {
        getServletContext().log("report servlet ready");
    }
}
