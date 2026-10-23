package com.example.legacy.web;

import javax.servlet.ServletContextEvent;
import javax.servlet.ServletContextListener;

public class AppStartupListener implements ServletContextListener {

    public void contextInitialized(ServletContextEvent event) {
        event.getServletContext().log("legacy webapp started");
    }

    public void contextDestroyed(ServletContextEvent event) {
        event.getServletContext().log("legacy webapp stopped");
    }
}
