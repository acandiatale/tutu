package com.example.fwapp.web;

import java.io.IOException;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.example.fw.core.FwResult;
import com.example.fwapp.service.GreetingService;

public class GreetingServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    private final transient GreetingService service = new GreetingService();

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        FwResult<String> result = service.greet(req.getParameter("name"));
        if (!result.isOk()) {
            resp.sendError(HttpServletResponse.SC_BAD_REQUEST, result.getCode());
            return;
        }
        resp.setContentType("text/plain;charset=UTF-8");
        resp.getWriter().println(result.getData());
    }
}
