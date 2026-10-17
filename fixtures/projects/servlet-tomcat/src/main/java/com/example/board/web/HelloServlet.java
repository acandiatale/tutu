package com.example.board.web;

import java.io.IOException;
import java.io.PrintWriter;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.example.board.model.Notice;
import com.example.board.service.NoticeService;

public class HelloServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    private transient NoticeService service;

    @Override
    public void init() throws ServletException {
        service = NoticeService.getInstance();
    }

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        resp.setContentType("text/plain;charset=UTF-8");
        PrintWriter out = resp.getWriter();
        out.println("Hello " + req.getParameter("name"));
        for (Notice n : service.list()) {
            out.println(n.getId() + " " + n.getTitle());
        }
    }

    @Override
    protected void doPost(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        req.setCharacterEncoding("UTF-8");
        Notice n = service.post(req.getParameter("title"), req.getParameter("body"));
        resp.sendRedirect(req.getContextPath() + "/hello?created=" + n.getId());
    }
}
