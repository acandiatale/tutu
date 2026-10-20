package com.example.catalog.web;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;

import com.example.catalog.service.CatalogService;

@Controller
@RequestMapping("/catalog")
public class CatalogController {

    private final CatalogService catalogService;

    @Autowired
    public CatalogController(CatalogService catalogService) {
        this.catalogService = catalogService;
    }

    @GetMapping
    public String show(Model model) {
        model.addAttribute("title", catalogService.getTitle());
        model.addAttribute("pageSize", catalogService.getPageSize());
        model.addAttribute("mailHost", catalogService.getMailHost());
        return "catalog";
    }
}
