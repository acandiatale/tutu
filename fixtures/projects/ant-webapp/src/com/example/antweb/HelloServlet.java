package com.example.antweb;

import java.io.IOException;
import java.io.PrintWriter;
import java.util.ResourceBundle;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import org.apache.commons.lang.StringUtils;

import com.example.util.Greeter;

/**
 * Registered in WebContent/WEB-INF/web.xml at /hello (no annotation, the classic Servlet 2.5 way).
 * Uses commons-lang 2.6 (a jar with Maven coordinates) and com.example.util.Greeter from example-util-1.0.jar,
 * an in-house jar without pom.properties that is in no repository.
 */
public class HelloServlet extends HttpServlet {

    private static final long serialVersionUID = 1L;

    private Greeter greeter;

    @Override
    public void init() throws ServletException {
        ResourceBundle messages = ResourceBundle.getBundle("com.example.antweb.messages");
        greeter = new Greeter(messages.getString("hello.salutation"));
    }

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws ServletException, IOException {
        String name = req.getParameter("name");
        name = StringUtils.isBlank(name) ? "world" : StringUtils.capitalize(StringUtils.trim(name));
        resp.setContentType("text/plain;charset=UTF-8");
        PrintWriter out = resp.getWriter();
        out.print(greeter.greet(name));
    }
}
