package com.example.vault.web;

import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseBody;

import com.example.vault.service.GreetingService;
import com.example.vault.util.CryptoUtil;

@Controller
public class HomeController {

    private final GreetingService greetingService;

    public HomeController(GreetingService greetingService) {
        this.greetingService = greetingService;
    }

    @GetMapping("/home")
    public String home() {
        return "index";
    }

    @GetMapping(value = "/api/greeting", produces = "text/plain")
    @ResponseBody
    public String greeting(@RequestParam(value = "name", required = false) String name) {
        return greetingService.greet(name);
    }

    @GetMapping(value = "/api/token", produces = "text/plain")
    @ResponseBody
    public String token(@RequestParam(value = "name", required = false) String name) {
        return CryptoUtil.encrypt(name);
    }
}
