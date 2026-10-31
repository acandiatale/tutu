package com.example.shop.service;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

@Service
public class NoticeService {

    @Value("${app.title}")
    private String title;

    @Value("${notice.page-size}")
    private int pageSize;

    public String title() {
        return title;
    }

    public int pageSize() {
        return pageSize;
    }
}
