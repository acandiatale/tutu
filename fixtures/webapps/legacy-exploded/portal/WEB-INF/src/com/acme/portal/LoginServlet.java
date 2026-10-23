package com.acme.portal;

import java.io.IOException;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

public class LoginServlet extends HttpServlet {
    private static final String MSG = "로그인에 실패했습니다";

    protected void doPost(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        if (DbUtil.check(req.getParameter("id"))) {
            resp.sendRedirect("index.jsp");
        } else {
            resp.getWriter().print(MSG);
        }
    }
}
