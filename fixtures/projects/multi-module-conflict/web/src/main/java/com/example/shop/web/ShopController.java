package com.example.shop.web;

import java.util.ArrayList;
import java.util.List;
import java.util.ResourceBundle;
import java.util.ServiceLoader;

import com.example.shop.shared.Shared;
import com.example.shop.spi.Plugin;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class ShopController {

    @GetMapping("/hello")
    public String hello(@RequestParam(defaultValue = "guest") String name) {
        return Shared.greet(name) + " (" + ResourceBundle.getBundle("messages").getString("shop.title") + ")";
    }

    @GetMapping("/bye")
    public String bye(@RequestParam(defaultValue = "guest") String name) {
        return Shared.farewell(name);
    }

    @GetMapping("/plugins")
    public List<String> plugins() {
        List<String> names = new ArrayList<>();
        for (Plugin plugin : ServiceLoader.load(Plugin.class)) {
            names.add(plugin.name());
        }
        return names;
    }
}
