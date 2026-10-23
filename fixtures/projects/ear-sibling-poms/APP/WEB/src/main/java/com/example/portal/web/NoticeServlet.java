package com.example.portal.web;

import com.example.portal.batch.NoticeCleaner;

import java.io.IOException;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/** Notice list; the admin "clean" button reuses the batch module's cleaner. */
public class NoticeServlet extends HttpServlet {

    private final NoticeCleaner cleaner = new NoticeCleaner();

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        req.setAttribute("cleaned", cleaner.clean(req.getParameter("reason")));
        req.getRequestDispatcher("/WEB-INF/jsp/notice/list.jsp").forward(req, resp);
    }
}
