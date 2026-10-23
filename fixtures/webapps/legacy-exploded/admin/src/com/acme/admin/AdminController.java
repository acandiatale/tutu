package com.acme.admin;

import javax.servlet.http.HttpServletRequest;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.RequestMapping;

import com.acme.common.Util;

@Controller
public class AdminController {
    @RequestMapping("/dashboard.do")
    public String dashboard(HttpServletRequest request) {
        return Util.isEmpty(request.getParameter("q")) ? "dashboard" : "users";
    }
}
