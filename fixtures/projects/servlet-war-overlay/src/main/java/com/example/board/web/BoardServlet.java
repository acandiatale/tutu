package com.example.board.web;

import java.io.IOException;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/** Board list page; its JSP includes the common header that the legacy-base-web overlay provides. */
public class BoardServlet extends HttpServlet {

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        req.setAttribute("title", "Board");
        req.getRequestDispatcher("/WEB-INF/jsp/board/list.jsp").forward(req, resp);
    }
}
