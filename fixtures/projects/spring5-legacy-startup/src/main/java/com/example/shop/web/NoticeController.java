package com.example.shop.web;

import com.example.shop.notice.NoticeFormatter;
import com.example.shop.service.NoticeService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.RequestMapping;

@Controller
public class NoticeController {

    @Autowired
    private NoticeService noticeService;

    @Autowired
    private NoticeFormatter noticeFormatter;

    @RequestMapping("/notice/list.do")
    public String list(Model model) {
        model.addAttribute("title", noticeService.title());
        model.addAttribute("pageSize", noticeService.pageSize());
        model.addAttribute("notice", noticeFormatter.format(noticeService.title()));
        return "notice/list";
    }
}
